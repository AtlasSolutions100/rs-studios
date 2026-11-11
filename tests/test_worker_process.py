"""The worker as its own process (roadmap M7.2, REL-4): workerproc.py, worker_main.py, tasks.py, the compose service.

    python tests/test_worker_process.py      (tests/test_app.py runs it too)

  * worker.mode(): process only when compose says so (RS_WORKER_MODE=process) and the kill switch is on
  * the web in process mode starts no loop, no plan sync, no nudges; planning, research and the free plan are
    queued instead of threads in gunicorn
  * the queue: compare-and-set claims (one winner), the run's own row must still be waiting, a dead worker's task
    is interrupted and never run again, the credit hold is rebuilt and kept alive by the worker
  * /health in process mode: ok only on a heartbeat from a worker of the same build (the deploy hook's check after
    the switch); the candidate (RS_WORKER=0) passes; the candidate fails when the worker's code can't start
  * the worker process itself (a real subprocess): one worker at a time (the lease), the heartbeat with its build
    and role, the beat file for its healthcheck, SIGTERM exits cleanly and lets the lease go, the kill switch idles
  * docker-compose: both services from one image, the worker after the app, a pre-M7.2 image only sleeps
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_workerproc_")
BEAT = os.path.join(TMP, "beat")
os.environ.update(RS_DATA=TMP, RS_WORKER="1", RS_WORKER_MODE="process", HF_KEY="test:secret", RS_PLAN_SYNC="0",
                  RS_EMAIL_VERIFY="0", RS_NUDGE_LOOP="0", RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", OPS_ALERTS="0",
                  RS_WORKER_BEAT_FILE=BEAT)
for k in ("RS_WORKER_PROCESS", "RS_PROC_ROLE", "AD_LAUNCH", "BACKUPS"):
    os.environ.pop(k, None)

import alerts  # noqa: E402
import worker  # noqa: E402

worker._build[:] = ["build-new"]
import app as appmod  # noqa: E402
import credits  # noqa: E402
import db  # noqa: E402
import planner  # noqa: E402
import tasks  # noqa: E402
import workerproc  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        shutil.rmtree(TMP, True)
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def q(sql, *a):
    with db.connect() as conn:
        return conn.execute(sql, a).fetchall()


def ex(sql, *a):
    with db.connect() as conn:
        cur = conn.execute(sql, a)
        conn.commit()
        return cur.lastrowid


# ================================================================ 1. the mode
print("the mode")
check(worker.mode() == "process", "RS_WORKER_MODE=process (set by docker-compose): process mode")
os.environ["RS_WORKER_PROCESS"] = "off"
check(worker.mode() == "thread", "RS_WORKER_PROCESS=off (the kill switch in settings.txt): back to the thread")
os.environ.pop("RS_WORKER_PROCESS")
os.environ["RS_WORKER_MODE"] = ""
check(worker.mode() == "thread", "no RS_WORKER_MODE (a local run, the deploy hook's candidate): the thread, as before")
os.environ["RS_WORKER_MODE"] = "process"
check(worker._thread is None and not any(t.name in ("rs-worker", "rs-plan-sync", "rs-first-run-nudges")
                                          for t in __import__("threading").enumerate()),
      "the web process in process mode started no loop")
check(not worker.background_here(), "and runs neither the plan sync nor the nudges")
os.environ["RS_PROC_ROLE"] = "worker"
check(worker.background_here(), "the worker process does")
os.environ.pop("RS_PROC_ROLE")

# ================================================================ 2. the queue
print("the queue")
cl = appmod.app.test_client()
cl.get("/login")
with cl.session_transaction() as s:
    tok = s["csrf"]
cl.post("/signup", data={"csrf": tok, "email": "workerproc@example.com", "password": "longpassword"})
U = q("SELECT * FROM users WHERE email='workerproc@example.com'")[0]
WS = U["workspace_id"]
ex("UPDATE workspaces SET credits=5000 WHERE id=?", WS)


def campaign(status="planning"):
    return ex("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, created_at, "
              "updated_at) VALUES (?,?,?,?,?,?,?,?,?)", WS, U["id"], "Plan", "{}", status, "{}", "[]", db.now(), db.now())


cid = campaign()
hold = credits.reserve(WS, 300, "AI campaign planning")
check(hold is not None and hold.id in credits._alive, "the web holds credits for the run")
r = planner.start(cid, "Make it better.", hold)
rows = q("SELECT * FROM task_queue")
check(r == "planning" and len(rows) == 1 and rows[0]["kind"] == "planner" and rows[0]["status"] == "queued"
      and rows[0]["workspace_id"] == WS and rows[0]["ref"] == f"campaign:{cid}",
      "planner.start queues the run and returns at once (no thread in gunicorn)")
p = json.loads(rows[0]["payload_json"])
check(p["cid"] == cid and p["notes"] == "Make it better." and p["hold"]["id"] == hold.id and p["hold"]["amount"] == 300,
      "the payload is ids and the hold's fields only")
check(hold.id not in credits._alive, "the web hands the hold's keepalive to the worker")

ran = []


def fake_safe_run(c, notes, h):
    ran.append((c, notes, h))
    ex("UPDATE campaigns SET status='ready' WHERE id=?", c)


real_safe_run = planner._safe_run
planner._safe_run = fake_safe_run
check(tasks.claim(db.connect(), rows[0]["id"], "w1") and not tasks.claim(db.connect(), rows[0]["id"], "w2"),
      "a claim is compare-and-set: one worker wins")
ex("UPDATE task_queue SET status='queued', claimed_by=NULL WHERE id=?", rows[0]["id"])
n = tasks.tick("w1", inline=True)
t = q("SELECT * FROM task_queue WHERE id=?", rows[0]["id"])[0]
check(n == 1 and t["status"] == "done" and t["claimed_by"] == "w1", "the worker runs it: done")
check(ran and ran[0][0] == cid and isinstance(ran[0][2], credits.Hold) and ran[0][2].id == hold.id
      and ran[0][2].amount == 300, "with the credit hold rebuilt")
check(hold.id in credits._alive, "and keeps the hold alive while it runs (the stale sweep leaves it)")
credits._done(hold.id)

cid2 = campaign()
planner.start(cid2, None, None)
ex("UPDATE campaigns SET status='failed' WHERE id=?", cid2)       # the page's stale check failed it meanwhile
ran.clear()
tasks.tick("w1", inline=True)
t = q("SELECT * FROM task_queue WHERE ref=?", f"campaign:{cid2}")[0]
check(t["status"] == "skipped" and not ran, "a run whose row stopped waiting is skipped, never run late")

cid3 = campaign()
tid = tasks.enqueue("planner", {"cid": cid3, "notes": None, "hold": None}, WS, f"campaign:{cid3}")
with db.connect() as conn:
    tasks.claim(conn, tid, "dead-worker")
    conn.execute("UPDATE task_queue SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00+00:00", tid))
    conn.commit()
    check(tasks.sweep(conn) == 1, "a running task whose lease ran out is found")
t = q("SELECT * FROM task_queue WHERE id=?", tid)[0]
ran.clear()
tasks.tick("w1", inline=True)
check(t["status"] == "interrupted" and not ran and q("SELECT status FROM task_queue WHERE id=?", tid)[0]["status"]
      == "interrupted", "a dead worker's task is interrupted and never run again (the stale check settles its row)")

import competitors  # noqa: E402
import paywall_routes  # noqa: E402

rid = ex("INSERT INTO competitor_reports (workspace_id, status, inputs_json, created_at, updated_at) VALUES (?,?,?,?,?)",
         WS, "running", json.dumps({"hold": None}), db.now(), db.now())
check(competitors.start(rid) == "running" and q("SELECT kind FROM task_queue WHERE ref=?", f"competitor:{rid}")[0]["kind"]
      == "competitors", "Competitor Snapshot runs are queued too")
cid4 = campaign()
check(paywall_routes.start(7, cid4, None) == "planning" and q("SELECT payload_json FROM task_queue WHERE kind='free_plan'"),
      "and the free plan")
with db.connect() as conn:
    c = tasks.counts(conn)
check(c.get("queued") == 2 and c.get("done") == 1, "counts for the System card")
ex("DELETE FROM task_queue WHERE status='queued'")
planner._safe_run = real_safe_run

# ================================================================ 3. /health in process mode
print("/health in process mode")
ex("DELETE FROM meta WHERE key IN (?, ?)", alerts.HEARTBEAT_META, alerts.BUILD_META)
alerts._beat.update(at=0.0, saved=0.0)
r = cl.get("/health")
check(r.status_code == 503 and r.get_json()["checks"]["worker"]["status"] in ("starting", "no heartbeat"),
      "no worker heartbeat yet: not ok")
ex("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", alerts.HEARTBEAT_META, str(time.time()))
ex("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", alerts.BUILD_META,
   json.dumps({"build": "build-old", "role": "worker"}))
r = cl.get("/health")
w = r.get_json()["checks"]["worker"]
check(r.status_code == 503 and w["status"] == "starting" and w["worker_build"] == "build-old" and w["build"] == "build-new",
      "a fresh beat from the OLD release's worker doesn't count: the hook's check after the switch waits")
alerts.STARTED -= 600
r = cl.get("/health")
check(r.status_code == 503 and r.get_json()["checks"]["worker"]["status"] == "no heartbeat",
      "and after the start window it says no heartbeat (the hook rolls back; the worker_down alert can fire)")
alerts.STARTED += 600
ex("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", alerts.BUILD_META,
   json.dumps({"build": "build-new", "role": "web"}))
check(cl.get("/health").status_code == 503, "a beat from a web thread of the same build doesn't count either")
ex("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", alerts.BUILD_META,
   json.dumps({"build": "build-new", "role": "worker"}))
r = cl.get("/health")
w = r.get_json()["checks"]
check(r.status_code == 200 and w["worker"]["status"] == "ok" and w["worker"]["mode"] == "process"
      and w["worker_code"] == "ok", "this release's worker beats: ok, mode process")
ex("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", alerts.HEARTBEAT_META, str(time.time() - 400))
check(cl.get("/health").status_code == 503, "a late beat fails it, as before")
os.environ.update(RS_WORKER="0", RS_HEALTH_WORKER_MAX_AGE="31536000")     # the deploy hook's candidate_env
r = cl.get("/health")
check(r.status_code == 200, "the deploy hook's candidate (RS_WORKER=0) passes on its own")
workerproc._ok.clear()
workerproc.LOOP_MODULES = workerproc.LOOP_MODULES + ("no_such_worker_module",)
r = cl.get("/health")
check(r.status_code == 503 and "no_such_worker_module" in r.get_json()["checks"]["worker_code"],
      "but fails when the worker's code can't start: the release never replaces the live one")
workerproc.LOOP_MODULES = workerproc.LOOP_MODULES[:-1]
workerproc._ok.clear()
check(cl.get("/health").status_code == 200, "fixed: passes")
os.environ["RS_WORKER"] = "1"
os.environ.pop("RS_HEALTH_WORKER_MAX_AGE")
worker._build[:] = [""]
ex("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", alerts.HEARTBEAT_META, str(time.time()))
check(cl.get("/health").status_code == 200, "no BUILD_ID (outside docker): the beat's age alone, as before")
worker._build[:] = ["build-new"]

ex("UPDATE users SET is_admin=1, email_verified_at=? WHERE id=?", db.now(), U["id"])
html = cl.get("/admin").get_data(as_text=True)
check('id="worker"' in html and "Its own container (rs-studios-worker)" in html and "AI runs (planning, research)" in html,
      "the admin System card says where the loop runs and what is queued")

# ================================================================ 4. the lease
print("the lease")
with db.connect() as conn:
    check(db.lease(conn, "t", "a", 60) and db.lease(conn, "t", "a", 60) and not db.lease(conn, "t", "b", 60),
          "one holder at a time; the holder renews")
    conn.execute("UPDATE worker_leases SET until='2000-01-01T00:00:00+00:00' WHERE name='t'")
    conn.commit()
    check(db.lease(conn, "t", "b", 60), "a lapsed lease can be taken")

# ================================================================ 5. the worker process (real subprocesses)
print("the worker process")
env = dict(os.environ, RS_WORKER="1", RS_WORKER_MODE="process", RS_POLL_SECONDS="0.2", RS_WORKER_STOP_S="2",
           PYTHONUNBUFFERED="1")
env.pop("RS_PROC_ROLE", None)
open(os.path.join(TMP, "BUILD_ID"), "w").write("x")     # (the image writes it next to the code; not here)
ex("DELETE FROM worker_leases")
ex("DELETE FROM meta WHERE key IN (?, ?)", alerts.HEARTBEAT_META, alerts.BUILD_META)
cid5 = campaign(status="ready")
skip_tid = tasks.enqueue("planner", {"cid": cid5, "notes": None, "hold": None}, WS, "campaign:x")
p1 = subprocess.Popen([sys.executable, os.path.join(ROOT, "worker_main.py")], cwd=ROOT, env=env,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(cond, s=30.0):
    end = time.time() + s
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


check(wait_for(lambda: bool(q("SELECT 1 FROM worker_leases WHERE name='worker'"))), "the worker takes the lease")
holder = q("SELECT holder FROM worker_leases WHERE name='worker'")[0]["holder"]
check(holder.endswith(f":{p1.pid}"), "under its own name (host:pid)")
check(wait_for(lambda: bool(q("SELECT 1 FROM meta WHERE key=?", alerts.BUILD_META))), "it beats")
info = json.loads(q("SELECT value FROM meta WHERE key=?", alerts.BUILD_META)[0]["value"])
check(info["role"] == "worker" and info["mode"] == "process", "the heartbeat says worker, process mode")
check(wait_for(lambda: q("SELECT status FROM task_queue WHERE id=?", skip_tid)[0]["status"] == "skipped"),
      "its pass runs the queue")
check(os.path.exists(BEAT) and time.time() - os.path.getmtime(BEAT) < 30, "and touches its beat file")
chk = subprocess.run([sys.executable, os.path.join(ROOT, "worker_main.py"), "--check"], cwd=ROOT, env=env,
                     capture_output=True, text=True)
check(chk.returncode == 0, "the container healthcheck passes")

p2 = subprocess.Popen([sys.executable, os.path.join(ROOT, "worker_main.py")], cwd=ROOT, env=env,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(4)
check(q("SELECT holder FROM worker_leases WHERE name='worker'")[0]["holder"] == holder and p2.poll() is None,
      "a second worker stands by while the first holds the lease")
p1.send_signal(signal.SIGTERM)
try:
    code = p1.wait(timeout=30)
except subprocess.TimeoutExpired:
    p1.kill()
    code = None
check(code == 0, "SIGTERM: the worker finishes its pass and exits 0")
check(wait_for(lambda: q("SELECT holder FROM worker_leases WHERE name='worker'")[0]["holder"].endswith(f":{p2.pid}"),
               20), "it lets the lease go and the standby worker takes over at once")
p2.send_signal(signal.SIGTERM)
check(p2.wait(timeout=30) == 0, "the second one stops cleanly too")

os.utime(BEAT, (time.time() - 600, time.time() - 600))
chk = subprocess.run([sys.executable, os.path.join(ROOT, "worker_main.py"), "--check"], cwd=ROOT, env=env,
                     capture_output=True, text=True)
check(chk.returncode == 1, "a stale beat file fails the container healthcheck")

ex("DELETE FROM worker_leases")
p3 = subprocess.Popen([sys.executable, os.path.join(ROOT, "worker_main.py")], cwd=ROOT,
                      env=dict(env, RS_WORKER_PROCESS="off"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
check(wait_for(lambda: time.time() - os.path.getmtime(BEAT) < 10), "kill switch off: the worker container stays up")
time.sleep(1)
check(not q("SELECT 1 FROM worker_leases") and p3.poll() is None, "and runs nothing (no lease, the web has the thread)")
p3.send_signal(signal.SIGTERM)
check(p3.wait(timeout=30) == 0, "and stops cleanly")
for pr in (p1, p2, p3):
    if pr.stdout:
        pr.stdout.close()

# ================================================================ 5b. the watchdog: a hung pass restarts the worker
print("the watchdog")
import threading  # noqa: E402

gone = []
hang = threading.Event()


def stuck_call():           # stands in for a vendor call that never returns
    hang.wait(30)


loop_thread = threading.Thread(target=stuck_call, name="fake-loop", daemon=True)
loop_thread.start()
ex("DELETE FROM meta WHERE key=?", workerproc.STALL_META)
now = time.time()
alerts._beat["at"] = now - 5000
workerproc._pass["started"] = 0.0
check(workerproc.stalled(now) == 0.0 and not workerproc.check_stall(loop_thread, 1200, gone.append, now) and not gone,
      "no pass running (between passes, or standing by for the lease): never restarted, however old the last beat")
workerproc._pass["started"] = now - 100
check(workerproc.stalled(now) == 100 and not workerproc.check_stall(loop_thread, 1200, gone.append, now) and not gone,
      "a pass 100 s in is left alone")
workerproc._pass["started"] = now - 3000
alerts._beat["at"] = now - 60
check(workerproc.stalled(now) == 60 and not workerproc.check_stall(loop_thread, 1200, gone.append, now) and not gone,
      "a long pass that still beats (a call inside alerts.busy) is left alone")
alerts._beat["at"] = now - 1300
alerts._busy.update(what="flows", since=now - 2200, depth=1)
check(workerproc.check_stall(loop_thread, 1200, gone.append, now) and gone == [workerproc.STALL_EXIT],
      "a pass with no beat for 1300 s is hung: the worker leaves with exit 70, and docker starts it again")
alerts._busy.update(what=None, since=0.0, depth=0)
with db.connect() as conn:
    st = workerproc.last_stall(conn)
check(st and st["age_s"] == 1300 and st["busy"] == "flows" and any("stuck_call" in x for x in st["stack"])
      and st["where"].endswith("stuck_call") and st["at_iso"].endswith("+00:00"),
      "and it wrote down where the loop was: the stage and the call stack")
check(all(re.fullmatch(r"[\w.<>-]+:\d+ [\w<>.]+", x) for x in st["stack"]),
      "the note is file:line function only: no source text and no values")
gone.clear()
check(not workerproc.check_stall(loop_thread, 0, gone.append, now) and not gone, "RS_WORKER_WATCHDOG_S=0 switches it off")
os.environ["RS_WORKER_WATCHDOG_S"] = "0"
check(workerproc.watchdog_limit() == 0 and workerproc.start_watchdog(loop_thread) is None, "and no watchdog thread starts")
os.environ.pop("RS_WORKER_WATCHDOG_S")
check(workerproc.watchdog_limit() == workerproc.WATCHDOG_S == 1200
      and workerproc.WATCHDOG_S > alerts._num("RS_HEALTH_BUSY_MAX_S", 900),
      "by default 20 minutes, longer than a busy call may keep the beat going")
workerproc._pass["started"] = 0.0
hang.set()
# the whole thing in a real process: a pass that never returns, and the worker leaves by itself
ex("DELETE FROM worker_leases")
ex("DELETE FROM meta WHERE key=?", workerproc.STALL_META)
hung = ("import time, worker, workerproc\n"
        "def never_returns():\n    time.sleep(600)\n"
        "worker.loop_once = never_returns\n"
        "workerproc.WATCHDOG_EVERY_S = 0.5\n"
        "raise SystemExit(workerproc.run())\n")
p4 = subprocess.Popen([sys.executable, "-c", hung], cwd=ROOT, env=dict(env, RS_WORKER_WATCHDOG_S="2"),
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    out4, _ = p4.communicate(timeout=60)
except subprocess.TimeoutExpired:
    p4.kill()
    out4, _ = p4.communicate()
with db.connect() as conn:
    st4 = workerproc.last_stall(conn)
check(p4.returncode == workerproc.STALL_EXIT and "restarting" in out4 and "never_returns" in out4,
      "a real worker whose pass never returns exits 70 within seconds of the limit, with every thread's stack in its log")
check(st4 and st4["where"].endswith("never_returns") and st4["age_s"] >= 2, "and leaves the note for the admin page")
ex("DELETE FROM worker_leases")
import admin_funnel  # noqa: E402
with db.connect() as conn:
    ws_ = admin_funnel._worker_status(conn)
check(ws_["stall"]["where"].endswith("never_returns"), "the admin page's worker row gets the last stall")
html = cl.get("/admin").get_data(as_text=True)
check('id="worker-stall"' in html and "Last restart by the watchdog" in html and "never_returns</code>" in html,
      "and the admin System card shows it")
ex("DELETE FROM meta WHERE key=?", workerproc.STALL_META)
check('id="worker-stall"' not in cl.get("/admin").get_data(as_text=True), "nothing is shown when it never had to")
src = open(os.path.join(ROOT, "workerproc.py")).read()
check("_pass[\"started\"] = time.time()" in src and "finally:\n                _pass[\"started\"] = 0.0" in src
      and "start_watchdog(threading.current_thread())" in src, "run() marks each pass and starts the watchdog")

# ================================================================ 6. the compose file and the image
print("docker-compose and the image")
compose = open(os.path.join(ROOT, "docker-compose.yml")).read()      # (no YAML parser in requirements: read as text)
svc = {m.group(1): m.group(2) for m in re.finditer(r"(?m)^  (\w+):\n((?:    .*\n|\s*\n)+)", compose)}
app_s, wk = svc.get("app", ""), svc.get("worker", "")
check(app_s.count("image: rs-studios-app:latest") == 1 and wk.count("image: rs-studios-app:latest") == 1
      and "build: ." not in wk, "both services run the image the deploy hook tags (only the app builds it)")
check("- RS_WORKER_MODE=process" in app_s and "- RS_WORKER_MODE=process" in wk and "- RS_PROC_ROLE=worker" in wk
      and "RS_PROC_ROLE" not in app_s, "process mode on both; the worker says so")
check("depends_on: [app]" in wk and "- ./data:/srv/rs/data" in wk and "env_file: [settings.txt]" in wk,
      "the worker starts after the app, with the same data and settings")
check("worker_main.py" in wk and "sleep infinity" in wk and "worker_main.py --check" in wk,
      "a pre-M7.2 image (after a rollback) has no worker_main.py: the worker only sleeps, its app runs the thread")
cmd = re.search(r'command: \["sh", "-c", "(.*)"\]', wk).group(1)
empty = tempfile.mkdtemp(dir=TMP)
out = subprocess.run(["sh", "-c", cmd.replace("exec sleep infinity", "exit 7")], cwd=empty, capture_output=True,
                     text=True, timeout=10)
check(out.returncode == 7 and "no worker process" in out.stdout, "(that branch, run in a folder without worker_main.py)")
check("container_name: rs-studios-worker" in wk and "stop_grace_period:" in wk,
      "its own container name and time to finish a pass on stop")
dockerfile = open(os.path.join(ROOT, "Dockerfile")).read()
check("BUILD_ID" in dockerfile and "token_hex" in dockerfile, "the image writes its BUILD_ID")

os.remove(os.path.join(TMP, "BUILD_ID"))
shutil.rmtree(TMP, True)
print(f"\nall {passed} checks passed")

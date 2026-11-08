"""Vendor retry and cancel (hf.py, worker.py, jobs_routes.py; roadmap M1.9: REL-2,
REL-7, BILL-5, observation 16), on a throwaway database with the vendor's HTTP
faked. Every path is driven by a scripted reply.

What it pins down:
  * a submit is retried on 408/425/429/5xx, timeouts and dropped connections (3
    tries, backoff with jitter, Retry-After up to 10 s), always with the same
    Idempotency-Key; any other 4xx is final at once; HF_RETRY=0 is one try;
  * the worker writes the job's idempotency key and a claim before calling the
    vendor, so a crash between submit and the status write resumes with the same
    key (the vendor hands back the first request) instead of paying twice, and a
    live claim is never sent twice;
  * a submit that stays busy goes back in the queue with a backoff and fails
    (refunded once) after 3 tries;
  * a timed-out job is canceled at the vendor, then failed and refunded;
  * the Cancel button (RS_JOB_CANCEL, off by default): a queued job is dropped and
    refunded; one the vendor hasn't started is canceled there and refunded; one
    already being made is left alone; a refund never happens twice.

    python tests/test_vendor_retry.py      (tests/test_app.py runs it too)
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_retry_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")  # sign-ups here go straight in; test_login.py checks the emailed-link step
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_ACCESS_LOG="0", RS_JOB_RETRY_SECONDS="30")
for k in ("HF_RETRY", "RS_JOB_CANCEL", "OPS_ALERT_EMAIL"):
    os.environ.pop(k, None)

import alerts  # noqa: E402
import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import requests  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


# ---------------------------------------------------------------- the fake vendor
class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return json.loads(self.text or "null")


class Crash(BaseException):
    """The process dies mid-call (not an Exception: nothing in the worker catches it)."""


V = {"script": [], "calls": [], "by_key": {}, "status": {}, "cancel": {}, "created": 0}
slept = []
hf._sleep = slept.append


def vendor_post(url, json=None, params=None, headers=None, timeout=0):  # noqa: A002
    V["calls"].append((url, dict(headers or {}), json))
    if url.endswith("/files/generate-upload-url"):
        r = V["script"].pop(0) if V["script"] and V.get("script_uploads") else None
        return r or Resp(200, {"upload_url": "https://up.example/x", "public_url": "https://files.example/p.png"})
    if "/requests/" in url and url.endswith("/cancel"):
        rid = url.split("/requests/")[1].split("/")[0]
        r = V["cancel"].get(rid, 202)
        if isinstance(r, BaseException):
            raise r
        if r == 202:
            V["status"][rid] = {"status": "canceled"}
        return Resp(r)
    step = V["script"].pop(0) if V["script"] else 200
    if isinstance(step, BaseException) or (isinstance(step, type) and issubclass(step, BaseException)):
        raise step
    if isinstance(step, Resp):
        return step
    if step != 200:
        return Resp(step, {"detail": f"vendor said {step}"})
    key = (headers or {}).get("Idempotency-Key")
    if key not in V["by_key"]:            # the vendor's idempotency: one request per key
        V["created"] += 1
        V["by_key"][key] = f"req-{V['created']}"
        V["status"][V["by_key"][key]] = {"status": "queued"}
    rid = V["by_key"][key]
    if V.get("crash_after_accept"):
        V["crash_after_accept"] = False
        raise Crash()
    return Resp(200, {"request_id": rid, "status": "queued"})


def vendor_get(url, headers=None, timeout=0):
    rid = url.split("/requests/")[1].split("/")[0]
    return Resp(200, V["status"][rid])


hf.requests.post, hf.requests.get = vendor_post, vendor_get


def reset(script=()):
    V["script"][:] = list(script)
    V["calls"].clear()
    slept.clear()


def submits():
    return [c for c in V["calls"] if "/requests/" not in c[0] and "/files/" not in c[0]]


# ---------------------------------------------------------------- hf.submit
print("== hf.submit retries")
reset([503, 502, 200])
rid = hf.submit("m/x", {"prompt": "a"}, idempotency_key="key-a")
keys = {c[1].get("Idempotency-Key") for c in submits()}
check(rid.startswith("req-") and len(submits()) == 3 and keys == {"key-a"},
      "503, 502, then 200: three tries, all with the same Idempotency-Key")
check(len(slept) == 2 and 0.5 <= slept[0] <= 1.0 and 1.0 <= slept[1] <= 2.0, f"backoff with jitter between tries {slept}")

reset([Resp(429, {"detail": "slow down"}, {"Retry-After": "3"}), 200])
hf.submit("m/x", {"prompt": "b"}, idempotency_key="key-b")
check(slept == [3.0] and len(submits()) == 2, "429 waits the Retry-After the vendor asked for")

reset([Resp(429, {"detail": "slow down"}, {"Retry-After": "120"})])
try:
    hf.submit("m/x", {"prompt": "c"}, idempotency_key="key-c")
    check(False, "a long Retry-After raises")
except hf.Transient as e:
    check(len(submits()) == 1 and not slept and e.retry_after == 120 and "busy" in str(e),
          "a Retry-After over 10 s isn't slept in the worker: Transient, left to the job's backoff")

for code in (408, 425, 500, 504):
    reset([code, code, code])
    try:
        hf.submit("m/x", {"prompt": str(code)}, idempotency_key=f"key-{code}")
        check(False, f"{code} x3 raises")
    except hf.Transient:
        check(len(submits()) == 3, f"{code} on every try: 3 tries, then Transient (try again later)")

for code in (400, 401, 402, 403, 404, 422):
    reset([code])
    try:
        hf.submit("m/x", {"prompt": str(code)}, idempotency_key=f"key-{code}")
        check(False, f"{code} raises")
    except hf.Transient:
        check(False, f"{code} is not transient")
    except hf.GenError as e:
        check(len(submits()) == 1 and not slept and str(e).startswith(str(code)), f"{code} is final at once (no retry)")

reset([requests.ConnectionError("reset by peer"), requests.Timeout("read timed out"), 200])
rid = hf.submit("m/x", {"prompt": "net"}, idempotency_key="key-net")
check(rid and len(submits()) == 3 and {c[1]["Idempotency-Key"] for c in submits()} == {"key-net"},
      "a dropped connection and a timeout are retried with the same key")
n_created = V["created"]
reset([200])
check(hf.submit("m/x", {"prompt": "net"}, idempotency_key="key-net") == rid and V["created"] == n_created,
      "resending a key returns the first request id; nothing new is made")

reset([200])
hf.submit("m/x", {"prompt": "nokey"})
check(len(submits()[0][1].get("Idempotency-Key") or "") == 32, "a submit without a key still sends one (its own retries dedupe)")

os.environ["HF_RETRY"] = "0"
reset([503, 200])
try:
    hf.submit("m/x", {"prompt": "off"}, idempotency_key="key-off")
    check(False, "HF_RETRY=0 doesn't retry")
except hf.GenError as e:
    check(len(submits()) == 1 and not isinstance(e, hf.Transient), "HF_RETRY=0: one try, a 503 fails as before")
os.environ.pop("HF_RETRY")

print("== hf.upload and hf.cancel")
V["script_uploads"] = True
reset([Resp(502, {"detail": "bad gateway"}), None])
check(hf.upload.__name__ == "upload", "real upload")
_put = hf.requests.put
hf.requests.put = lambda url, data=None, headers=None, timeout=0: Resp(200)
check(hf.upload(b"x", "image/png") == "https://files.example/p.png" and len(V["calls"]) == 2,
      "asking for an upload URL is retried after a 502")
hf.requests.put = _put
V["script_uploads"] = False
V["status"]["r-q"] = {"status": "queued"}
outcomes = {}
for rid, reply in (("r-q", 202), ("r-run", 400), ("r-none", 404), ("r-err", 500), ("r-net", requests.ConnectionError("x"))):
    V["cancel"][rid] = reply
    reset()
    outcomes[rid] = hf.cancel(rid)
check(outcomes["r-q"] == hf.CANCELED and outcomes["r-run"] == hf.STARTED and outcomes["r-none"] == hf.GONE,
      "cancel: 202 canceled, 400 already started, 404 gone")
check(outcomes["r-err"].startswith("error") and outcomes["r-net"].startswith("error"),
      f"cancel never raises: a 500 or a dropped connection is an error outcome ({outcomes['r-err']}, {outcomes['r-net']})")

# ---------------------------------------------------------------- the worker
print("== worker: busy vendor, crash between submit and status write")
now = db.now()
with db.connect() as conn:
    WS = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES ('T', 10000, ?)", (now,)).lastrowid
    UID = conn.execute("INSERT INTO users (email, pw_hash, workspace_id, created_at, email_verified_at) "
                       "VALUES ('t@example.com','x',?,?,?)", (WS, now, now)).lastrowid
    PH = conn.execute("INSERT INTO photos (workspace_id, path, content_type, remote_url, created_at) "
                      "VALUES (?, '/nope.jpg', 'image/jpeg', 'https://cdn.example/p.jpg', ?)", (WS, now)).lastrowid
    conn.commit()


def queue(tool="photo_to_video", ws=None):
    ws = ws or WS
    with db.connect() as conn:
        ph = conn.execute("SELECT id FROM photos WHERE workspace_id=?", (ws,)).fetchone()
        ph = ph["id"] if ph else conn.execute(
            "INSERT INTO photos (workspace_id, path, content_type, remote_url, created_at) "
            "VALUES (?, '/nope.jpg', 'image/jpeg', 'https://cdn.example/p.jpg', ?)", (ws, now)).lastrowid
        conn.commit()
    params = {"photo_ids": [ph], "duration": 5}
    credits = catalog.price_credits(tool, duration=5)
    with db.connect() as conn:
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                           (ws, UID, tool, "A mug on a table", json.dumps(params), credits, 1.0, now, now)).lastrowid
        db.adjust_credits(conn, ws, -credits, "generation", jid)
        conn.commit()
    return jid


def job(jid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def refunds(jid):
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM ledger WHERE job_id=? AND delta>0", (jid,)).fetchone()[0]


def bal(ws=None):
    with db.connect() as conn:
        return conn.execute("SELECT credits FROM workspaces WHERE id=?", (ws or WS,)).fetchone()[0]


def backdate(jid, col, minutes):
    with db.connect() as conn:
        conn.execute(f"UPDATE jobs SET {col}=? WHERE id=?", (worker._minutes_ago(minutes), jid))
        conn.commit()


def park(jid):
    """Out of the poll loop so later ticks only see the job under test."""
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='done' WHERE id=? AND status IN ('pending','running')", (jid,))
        conn.commit()


b0 = bal()
j1 = queue()
reset([503, 503, 503])
worker.tick()
r = job(j1)
check(r["status"] == "pending" and r["attempts"] == 1 and r["submit_key"] and r["claimed_at"] is None
      and "problem on its side" in (r["error"] or "") and refunds(j1) == 0,
      "a vendor that stays down: back in the queue with its key, no refund yet")
key1 = r["submit_key"]
reset([200])
worker.tick()
check(not submits() and job(j1)["status"] == "pending", "and it waits out its backoff (not resent on the next tick)")
backdate(j1, "updated_at", 1)
worker.tick()
r = job(j1)
check(r["status"] == "running" and r["request_id"] and submits()[0][1]["Idempotency-Key"] == key1
      and r["error"] is None and r["claimed_at"] is None, "after the backoff it's resent with the same key and runs")
park(j1)

j2 = queue()
for i in range(worker.MAX_ATTEMPTS):
    reset([Resp(429, {"detail": "busy"}, {"Retry-After": "1"})] * 3)
    backdate(j2, "updated_at", 10)
    worker.tick()
r = job(j2)
check(r["status"] == "failed" and refunds(j2) == 1 and "busy" in r["error"] and r["error"].endswith("Credits refunded."),
      "busy on all 3 tries of all 3 attempts: failed, refunded once, says the service was busy")

j3 = queue()
reset([400])
worker.tick()
check(job(j3)["status"] == "failed" and refunds(j3) == 1 and len(submits()) == 1, "a 400 fails at once and is refunded")

waiting = [queue() for _ in range(6)]
reset([503] * 18)
worker.tick()
worker.tick()
check(all(job(j)["status"] == "pending" and job(j)["attempts"] == 1 for j in waiting),
      "6 jobs hit a busy vendor: all back in the queue (5 per pass)")
fresh = queue()
reset([200])
worker.tick()
check(job(fresh)["status"] == "running", "jobs waiting out a backoff don't hold up a new one behind them")
for j in waiting + [fresh]:
    park(j)

j4 = queue()
reset([200])
V["crash_after_accept"] = True
made = V["created"]
try:
    worker.tick()
    check(False, "the crash propagates")
except Crash:
    pass
r = job(j4)
check(r["status"] == "pending" and r["submit_key"] and r["claimed_at"] and V["created"] == made + 1,
      "crash after the vendor accepted it: the job still says pending, but its key and claim were written first")
reset([200])
worker.tick()
check(not submits() and job(j4)["status"] == "pending", "a live claim is never sent a second time")
backdate(j4, "claimed_at", worker.SUBMIT_LEASE_MINUTES + 1)
backdate(j4, "updated_at", worker.SUBMIT_LEASE_MINUTES + 1)
worker.tick()
r = job(j4)
check(r["status"] == "running" and V["created"] == made + 1 and r["request_id"] == V["by_key"][r["submit_key"]]
      and len(submits()) == 1, "after the lease, it resumes with the same key: the first request, not a second paid one")
park(j4)

with db.connect() as conn:
    j5 = queue()
    first = worker._claim(conn, job(j5))
    second = worker._claim(conn, job(j5))
check(first is not None and second is None, "two passes can't both claim one job")
with db.connect() as conn:
    conn.execute("UPDATE jobs SET claimed_at=NULL WHERE id=?", (j5,))
    conn.commit()
park(j5)

print("== worker: timeouts settle at the vendor")
j6 = queue()
reset([200])
worker.tick()
rid6 = job(j6)["request_id"]
V["status"][rid6] = {"status": "queued"}
V["cancel"][rid6] = 202
backdate(j6, "updated_at", worker.STUCK_MINUTES * 4 + 1)
reset()
worker.tick()
r = job(j6)
check(r["status"] == "failed" and refunds(j6) == 1 and "Timed out" in r["error"]
      and [c[0] for c in V["calls"]] == [f"{hf.BASE}/requests/{rid6}/cancel"],
      "a job the vendor never started: canceled at the vendor, then failed and refunded")

j7 = queue()
reset([200])
worker.tick()
rid7 = job(j7)["request_id"]
V["status"][rid7] = {"status": "in_progress"}
V["cancel"][rid7] = 400
backdate(j7, "updated_at", worker.STUCK_MINUTES * 4 + 1)
reset()
worker.tick()
check(job(j7)["status"] == "failed" and refunds(j7) == 1 and any(c[0].endswith(f"/{rid7}/cancel") for c in V["calls"]),
      "one already being made can't be stopped: still failed and refunded (the miss is logged with its request id)")

j8 = queue()
reset([200])
worker.tick()
rid8 = job(j8)["request_id"]
V["cancel"][rid8] = 404
_get = hf.requests.get
hf.requests.get = lambda url, headers=None, timeout=0: Resp(404, {"detail": "not found"})
backdate(j8, "updated_at", worker.STUCK_MINUTES * 4 + 1)
reset()
worker.tick()
hf.requests.get = _get
check(job(j8)["status"] == "failed" and refunds(j8) == 1 and any(c[0].endswith(f"/{rid8}/cancel") for c in V["calls"]),
      "a status that can never be read times out the same way: cancel asked, refunded once")
check(bal() == b0 - sum(job(j)["credits"] for j in [j1, j4, j5, fresh] + waiting),
      "the balance is right: only the jobs that ran (or were parked) are spent")

# ---------------------------------------------------------------- Cancel button
print("== the Cancel button (RS_JOB_CANCEL)")
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    with db.connect() as conn:
        ws = conn.execute("SELECT w.id FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                          (email,)).fetchone()["id"]
        conn.execute("UPDATE workspaces SET credits=5000 WHERE id=?", (ws,))
        conn.commit()
    return c, ws


A, AWS = signup("a@example.com")
B, BWS = signup("b@example.com")
ja = queue(ws=AWS)


def post_cancel(c, jid, js=True):
    hdr = {"Accept": "application/json"} if js else {}
    return c.post(f"/app/jobs/{jid}/cancel", data={"csrf": csrf(c)}, headers=hdr)


check(post_cancel(A, ja).status_code == 404 and job(ja)["status"] == "pending", "off by default: no cancel route")
check('/cancel"' not in A.get("/app/library").get_data(as_text=True), "and no Cancel button on the card")
os.environ["RS_JOB_CANCEL"] = "on"
lib = A.get("/app/library").get_data(as_text=True)
check(f'/app/jobs/{ja}/cancel"' in lib and "Cancel this ad?" in lib, "on: a queued card has a Cancel button that asks first")
check(post_cancel(B, ja).status_code == 404 and job(ja)["status"] == "pending", "another workspace can't cancel it")

ba = bal(AWS)
r = post_cancel(A, ja)
check(r.status_code == 200 and r.get_json()["ok"] and job(ja)["status"] == "failed"
      and job(ja)["error"] == "You canceled it. Credits refunded." and bal(AWS) == ba + job(ja)["credits"]
      and refunds(ja) == 1, "a queued job is dropped and refunded")
r = post_cancel(A, ja)
check(r.status_code == 409 and refunds(ja) == 1, "canceling twice refunds once")
jc = queue(ws=AWS)
with db.connect() as conn:
    stale = conn.execute("SELECT * FROM jobs WHERE id=?", (jc,)).fetchone()
post_cancel(A, jc)
with db.connect() as conn:
    check(worker._fail(conn, stale, "Timed out.") is False and refunds(jc) == 1,
          "the worker failing a row it read before the cancel doesn't refund it a second time")

jb = queue(ws=AWS)
with db.connect() as conn:
    worker._claim(conn, job(jb))
r = post_cancel(A, jb)
check(r.status_code == 409 and "starting right now" in r.get_json()["error"] and job(jb)["status"] == "pending"
      and refunds(jb) == 0, "a job being sent right now isn't canceled under the worker's feet")
with db.connect() as conn:
    conn.execute("UPDATE jobs SET claimed_at=NULL WHERE id=?", (jb,))
    conn.commit()
backdate(jb, "updated_at", 1)
reset([200])
worker.tick()
rb = job(jb)["request_id"]
check(job(jb)["status"] == "running", "(it runs)")
V["cancel"][rb] = 400
r = post_cancel(A, jb)
check(r.status_code == 409 and "can't be stopped" in r.get_json()["error"] and job(jb)["status"] == "running"
      and refunds(jb) == 0, "one the vendor is already making is left alone, not refunded")
V["cancel"][rb] = 500
r = post_cancel(A, jb)
check(r.status_code == 409 and "Try again" in r.get_json()["error"] and job(jb)["status"] == "running",
      "a vendor error on cancel leaves it running and says try again")
V["cancel"][rb] = 202
ba = bal(AWS)
r = A.post(f"/app/jobs/{jb}/cancel", data={"csrf": csrf(A), "back": "/app/library"})
check(r.status_code == 302 and job(jb)["status"] == "failed" and bal(AWS) == ba + job(jb)["credits"] and refunds(jb) == 1,
      "one the vendor hadn't started is canceled there and refunded (the form posts back to the library)")
reset()
worker.tick()
check(refunds(jb) == 1 and not V["calls"], "the worker doesn't poll or refund it again")

os.environ["OPS_VENDOR_MIN_JOBS"] = "1"
with db.connect() as conn:       # only the 3 cancels ended in the last 15 minutes
    conn.execute("UPDATE jobs SET finished_at='2020-01-01T00:00:00+00:00', updated_at='2020-01-01T00:00:00+00:00' "
                 "WHERE COALESCE(error, '') NOT LIKE 'You canceled it.%'")
    conn.commit()
    fired = [c[0] for c in alerts._conditions(conn, probe=False)]
    conn.execute("UPDATE jobs SET error='Timed out. Credits refunded.' WHERE id=?", (jb,))
    conn.commit()
    control = [c[0] for c in alerts._conditions(conn, probe=False)]
os.environ.pop("OPS_VENDOR_MIN_JOBS")
check("vendor_errors" not in fired and "vendor_errors" in control,
      "members' cancels don't count as vendor errors in the operator alert (a real failure still does)")

print("== a cancel that lands while the worker is finishing the job (N11)")
with db.connect() as conn:       # only the job under test in the poll loop
    conn.execute("UPDATE jobs SET status='done' WHERE status IN ('pending','running')")
    conn.commit()
jn = queue(ws=AWS)
reset()
worker.tick()
rn = job(jn)["request_id"]
check(job(jn)["status"] == "running" and rn, "submitted: running")
V["status"][rn] = {"status": "completed"}
V["cancel"][rn] = 404               # the vendor no longer knows it: the member's cancel refunds it
_save = worker._save_outputs


def save_then_cancel(conn, j, res):
    with db.connect() as c2:
        ok, _ = worker.cancel_job(c2, job(j["id"]))
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (j["id"], "video", "https://x/v.mp4", None))
    conn.commit()
    V["n11_cancel"] = ok


worker._save_outputs = save_then_cancel
ba = bal(AWS)
worker.tick()
worker._save_outputs = _save
with db.connect() as conn:
    outs = conn.execute("SELECT COUNT(*) FROM outputs WHERE job_id=?", (jn,)).fetchone()[0]
check(V["n11_cancel"] and job(jn)["status"] == "failed" and refunds(jn) == 1 and bal(AWS) == ba + job(jn)["credits"]
      and outs == 0, "the cancel wins: the job stays failed and refunded once, its outputs are dropped")
jm = queue(ws=AWS)
reset()
worker.tick()
rm = job(jm)["request_id"]
_status = hf.status


def cancel_then_completed(rid):
    # the member cancels after the worker read the job as running, before the result is in
    with db.connect() as c2:
        worker._fail(c2, job(jm), worker.CANCELED)
    return {"status": "completed"}


called = []
worker._save_outputs = lambda conn, j, res: called.append(j["id"])
hf.status = cancel_then_completed
worker.tick()
hf.status, worker._save_outputs = _status, _save
check(job(jm)["status"] == "failed" and not called and refunds(jm) == 1,
      "a job already canceled isn't saved or marked done by a later completion")

os.environ.pop("RS_JOB_CANCEL")
print(f"\nall {passed} checks passed ({TMP})")

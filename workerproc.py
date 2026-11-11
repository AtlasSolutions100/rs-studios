"""The worker as its own process (roadmap M7.2, REL-4).

docker-compose runs two containers from the same image: `app` (gunicorn, requests only) and `worker`
(`python worker_main.py`, everything in the background). Both get RS_WORKER_MODE=process from the compose file;
the worker also gets RS_PROC_ROLE=worker. The web process then starts no loop at all (worker.start, the plan sync
and the nudge loop check worker.background_here()), and planning, Competitor Snapshot and free-plan runs go through
the task queue (tasks.py) instead of threads in gunicorn. Slow vendor and AI calls never hold a request thread, and
a web restart never kills a run.

run():
  * one worker at a time: the lease "worker" in worker_leases (db.lease), renewed every pass. A second worker
    (a deploy overlap, a stray `docker compose run`) stands by until the lease lapses (WORKER_LEASE_S, 90 s);
  * every RS_POLL_SECONDS (5): worker.loop_once(), the same pass the thread ran (jobs, flows and sends,
    conversions, alerts, purge, stale holds, ad sweeps, launches, changes, insights, backups) plus tasks.tick();
  * the hourly plan sync and the nudge loop as threads of this process (importing app starts them);
  * the heartbeat (alerts.beat) carries this image's BUILD_ID, so the web of a new release answers /health ok only
    once its own worker beats (the deploy hook's check after the switch rolls back a release whose worker never
    comes up), and touches /tmp/rs-worker-beat for the container's own healthcheck;
  * SIGTERM (docker stop, a deploy): finish the pass, wait up to RS_WORKER_STOP_S (40) for running tasks, mark
    the rest interrupted, release the lease, exit 0;
  * a watchdog (a side thread): a pass that has shown no sign of life for RS_WORKER_WATCHDOG_S (1200 s) is hung.
    The loop is one thread, so one call that never returns stops every job, send and alert until someone
    restarts the container; the container's healthcheck only marks it unhealthy. The watchdog writes down where
    the loop is stuck (meta worker_last_stall: the stage, the call stack, no data), prints every thread's stack
    to the log, and exits 70, and `restart: unless-stopped` brings the worker back. It only watches a pass that
    is running: a worker standing by for the lease is never restarted. "0" switches it off.

Kill switch RS_WORKER_PROCESS=off (settings.txt): worker.mode() is "thread" again, the web runs the loop as a
thread as before, and this container only idles (it imports nothing of the app and touches its beat file).

selfcheck() is what /health adds for the deploy hook's candidate check before the switch: the candidate container
(RS_WORKER=0) never runs the loop, but its /health fails when the worker's code doesn't import or its tables are
missing, so a release whose worker can't start never replaces the live one.
"""
import logging
import os
import signal
import threading
import time

log = logging.getLogger("campaignify.workerproc")

WORKER_LEASE_S = 90.0
WATCHDOG_S = 1200.0             # RS_WORKER_WATCHDOG_S: no beat for this long inside a pass = hung. 0 = off
WATCHDOG_EVERY_S = 30.0
STALL_META = "worker_last_stall"
STALL_EXIT = 70
_pass = {"started": 0.0}        # when the pass now running began (0 between passes and while standing by)
LOOP_MODULES = ("worker", "tasks", "alerts", "flows_engine", "conversions", "storage", "credits", "adplatforms",
                "launches", "admanage", "adinsights", "backup", "nudges", "planner", "competitors", "paywall_routes",
                "wsdelete")
_stop = threading.Event()
_ok = []


def selfcheck() -> tuple[bool, str]:
    """Does everything the worker process runs import, and are its tables there? (Cached once it passes.)"""
    if _ok:
        return True, "ok"
    import importlib
    for m in LOOP_MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            return False, f"{m} doesn't import: {type(e).__name__}"
    try:
        import db
        with db.connect() as conn:
            for t in ("task_queue", "worker_leases"):
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                    return False, f"table {t} is missing"
    except Exception as e:  # noqa: BLE001
        return False, f"database: {type(e).__name__}"
    _ok.append(True)
    return True, "ok"


def _on_term(signum, frame):
    _stop.set()


def _touch():
    import alerts
    try:
        with open(alerts.BEAT_FILE, "a"):
            os.utime(alerts.BEAT_FILE, None)
    except OSError:
        pass


def watchdog_limit() -> float:
    """Seconds a running pass may go without a beat before the worker restarts itself (0 = never)."""
    try:
        return max(0.0, float(os.environ.get("RS_WORKER_WATCHDOG_S") or WATCHDOG_S))
    except ValueError:
        return WATCHDOG_S


def stalled(now: float | None = None) -> float:
    """How long the pass now running has shown no sign of life, in seconds: since its last beat (alerts.beat,
    which a long call inside alerts.busy keeps up for RS_HEALTH_BUSY_MAX_S), or since it began. 0 when no pass
    is running (between passes, or standing by for the lease)."""
    import alerts
    began = _pass["started"]
    if not began:
        return 0.0
    return max(0.0, (now or time.time()) - max(began, alerts._beat["at"] or 0.0))


def stall_report(thread, age: float) -> dict:
    """Where the loop's thread is right now: the stage (alerts.busy's label), and its call stack as
    "file:line function" lines, innermost last. No source text and no values, so nothing of a member's is in it."""
    import alerts
    import sys
    import traceback
    frame = sys._current_frames().get(getattr(thread, "ident", None))
    stack = [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in traceback.extract_stack(frame)] if frame else []
    return {"at": time.time(), "age_s": round(age), "busy": alerts._busy.get("what") or "",
            "stack": stack[-14:], "pid": os.getpid()}


def _save_stall(report: dict) -> None:
    import json

    import db
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (STALL_META, json.dumps(report)))
        conn.commit()


def last_stall(conn) -> dict | None:
    """The last time the watchdog restarted the worker: {"at", "age_s", "busy", "stack", "where"} or None."""
    import json
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (STALL_META,)).fetchone()
        d = json.loads(row["value"]) if row and row["value"] else None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict) or not d.get("at"):
        return None
    stack = [str(x) for x in d.get("stack") or []]
    # the innermost frame of ours says more than the library call under it
    ours = [x for x in stack if not x.startswith(("threading.py", "socket.py", "ssl.py", "client.py", "connectionpool.py",
                                                  "connection.py", "sessions.py", "adapters.py", "api.py", "subprocess.py",
                                                  "selectors.py", "response.py"))]
    d["where"] = (ours or stack or [""])[-1]
    d["stack"] = stack
    try:
        from datetime import datetime, timezone
        d["at_iso"] = datetime.fromtimestamp(float(d["at"]), tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError, OSError):
        d["at_iso"] = ""
    return d


def check_stall(thread, limit: float, exit_fn=os._exit, now: float | None = None) -> bool:
    """One look by the watchdog. A pass with no sign of life for `limit` seconds: record where it is, print every
    thread's stack, and leave (exit_fn(STALL_EXIT)); docker restarts the container. True when it did."""
    age = stalled(now)
    if not limit or age <= limit:
        return False
    report = stall_report(thread, age)
    log.error("worker pass hung for %s s (busy: %s) at %s; restarting", report["age_s"], report["busy"] or "-",
              " < ".join(reversed(report["stack"][-6:])) or "?")
    try:
        import faulthandler
        import sys
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    # the database may be what is stuck: never let the note keep the restart waiting
    saver = threading.Thread(target=lambda: _save_stall(report), name="rs-worker-stall-note", daemon=True)
    saver.start()
    saver.join(10)
    exit_fn(STALL_EXIT)
    return True


def start_watchdog(thread, exit_fn=os._exit):
    """The side thread that looks every WATCHDOG_EVERY_S. None when RS_WORKER_WATCHDOG_S is 0."""
    limit = watchdog_limit()
    if not limit:
        return None

    def watch():
        while not _stop.wait(WATCHDOG_EVERY_S):
            try:
                if check_stall(thread, limit, exit_fn):
                    return
            except Exception:  # noqa: BLE001
                log.exception("worker watchdog look failed")

    t = threading.Thread(target=watch, name="rs-worker-watchdog", daemon=True)
    t.start()
    return t


def idle() -> int:
    """RS_WORKER_PROCESS=off: the web runs the loop (thread mode); stay up and healthy, do nothing."""
    log.warning("worker process idle: RS_WORKER_PROCESS is off (or RS_WORKER_MODE isn't process), the web runs the loop")
    while not _stop.is_set():
        _touch()
        _stop.wait(30)
    return 0


def run(max_passes: int | None = None) -> int:
    """The worker container's main loop. max_passes is for tests."""
    os.environ["RS_PROC_ROLE"] = "worker"
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    import worker
    if worker.mode() != "process":
        return idle()
    import app  # noqa: F401  db.init, blueprints; starts this process's plan sync and nudge threads (background_here)
    import alerts
    import db
    import tasks
    name = tasks.worker_name()
    worker._thread = threading.current_thread()     # alerts.busy and the heartbeat know the loop's thread
    poll = worker.POLL_SECONDS
    holding, passes = False, 0
    log.warning("worker process %s up (build %s)", name, worker.build_id() or "none")
    if max_passes is None:
        start_watchdog(threading.current_thread())
    while not _stop.is_set():
        try:
            with db.connect() as conn:
                have = db.lease(conn, "worker", name, WORKER_LEASE_S, info=worker.build_id() or None)
        except Exception:  # noqa: BLE001
            log.exception("worker lease check failed")
            have = False
        if have != holding:
            log.warning("worker %s %s the loop", name, "runs" if have else "stands by: another worker holds")
            holding = have
        if have:
            _pass["started"] = time.time()
            try:
                worker.loop_once()
            finally:
                _pass["started"] = 0.0
        else:
            _touch()     # alive and waiting: the container is healthy
        passes += 1
        if max_passes is not None and passes >= max_passes:
            break
        _stop.wait(poll)
    stop_s = float(os.environ.get("RS_WORKER_STOP_S") or 40)
    left = tasks.wait(stop_s) if tasks.busy() else 0
    if left:
        n = tasks.release(name)
        log.warning("worker %s stopped with %s run(s) unfinished; marked interrupted", name, n)
    try:
        with db.connect() as conn:
            conn.execute("UPDATE worker_leases SET until=? WHERE name='worker' AND holder=?", ("1970-01-01T00:00:00+00:00",
                                                                                              name))
            conn.commit()
    except Exception:  # noqa: BLE001
        pass
    alerts.beat()
    return 0


def check() -> int:
    """The worker container's healthcheck: its beat file is fresh (RS_HEALTH_WORKER_MAX_AGE, 180 s)."""
    import alerts
    try:
        age = time.time() - os.path.getmtime(alerts.BEAT_FILE)
    except OSError:
        print("no heartbeat yet")
        return 1
    max_age = float(os.environ.get("RS_HEALTH_WORKER_MAX_AGE") or 180)
    print(f"last beat {age:.0f} s ago")
    return 0 if age <= max_age else 1

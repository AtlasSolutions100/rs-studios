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
    the rest interrupted, release the lease, exit 0.

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
LOOP_MODULES = ("worker", "tasks", "alerts", "flows_engine", "conversions", "storage", "credits", "adplatforms",
                "launches", "admanage", "adinsights", "backup", "nudges", "planner", "competitors", "paywall_routes")
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
            worker.loop_once()
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

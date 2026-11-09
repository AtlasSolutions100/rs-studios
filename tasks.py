"""The task queue: long AI runs the web process hands to the worker process (roadmap M7.2, REL-4).

With the worker as its own process (RS_WORKER_MODE=process, workerproc.py), a request that starts a campaign
planning run, a Competitor Snapshot run or the free plan no longer starts a thread inside gunicorn: it writes one
task_queue row and answers at once. The worker claims the row and runs it in one of its RS_TASK_THREADS (4)
threads, so a slow vendor or AI call never holds a request thread and a web restart never kills a run.

Claims work like Crowdcut's worker queue (render.claim, orphans.py): one compare-and-set UPDATE takes a queued row
(status queued -> running, claimed_by = the worker's name, lease_until = now + LEASE_S), so two workers can never
run the same task. The worker renews the lease of what it runs every RENEW_S. A running task whose lease ran out
(its worker died: deploy, crash, OOM) is marked interrupted by sweep(): it is NOT run again, because the run's own
row (campaigns, competitor_reports) already says how far it got and the pages' existing stale check (planner and
competitor STALE_MINUTES) marks it failed and gives the held credits back, exactly as when a thread died before.
A queued task whose run row is no longer waiting (failed by that stale check, deleted) is skipped, never run late.

In thread mode (the default outside docker-compose; RS_WORKER_PROCESS=off) nothing is queued: planner.start and
competitors.start keep their threads, and tests (RS_WORKER=0) run inline as before.

    kinds   planner      {"cid", "notes", "hold": credits.Hold fields}     planner.run
            competitors  {"rid"}                                            competitors.run
            free_plan    {"claim", "cid", "rid"}                            paywall_routes.run
"""
import json
import logging
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("campaignify.tasks")

LEASE_S = 180.0          # a running task's claim lapses this long after the last renewal
RENEW_S = 60.0
KEEP_DAYS = 14           # finished rows kept for the System card
KINDS = ("planner", "competitors", "free_plan")

_running = {}            # task id -> thread (this process)
_lock = threading.Lock()
_renewer = [None]


def threads() -> int:
    try:
        return max(1, min(16, int(os.environ.get("RS_TASK_THREADS") or 4)))
    except ValueError:
        return 4


def worker_name() -> str:
    """This worker's claim name: host (the container id in docker) and pid."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _iso(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- the web side

def enqueue(kind: str, payload: dict, workspace_id: int | None = None, ref: str | None = None) -> int:
    """Queue one run for the worker. Returns the task id."""
    assert kind in KINDS, kind
    t = db.now()
    with db.connect() as conn:
        tid = conn.execute("INSERT INTO task_queue (kind, payload_json, workspace_id, ref, status, attempts, created_at, "
                           "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                           (kind, json.dumps(payload), workspace_id, ref, "queued", 0, t, t)).lastrowid
        conn.commit()
    return tid


def hold_fields(hold) -> dict | None:
    """A credits.Hold as plain fields for the payload (the worker rebuilds it)."""
    if hold is None:
        return None
    return {"id": hold.id, "workspace_id": hold.workspace_id, "amount": hold.amount, "label": hold.label,
            "post_paid": bool(getattr(hold, "post_paid", False))}


def handoff(hold):
    """The web stops keeping the hold alive: the worker does it from its claim on (credits keepalive, N13)."""
    if hold is not None and hold.id is not None:
        import credits
        credits._done(hold.id)


# ---------------------------------------------------------------- the worker side

def _rebuild_hold(h):
    if not h:
        return None
    import credits
    hold = credits.Hold(h.get("id"), h.get("workspace_id"), h.get("amount") or 0, h.get("label") or "",
                        post_paid=bool(h.get("post_paid")))
    if hold.id is not None:
        credits._keep_alive(hold.id)
    return hold


def still_wanted(conn, kind: str, p: dict) -> bool:
    """Is the run's own row still waiting for this task? (A stale check may have failed it meanwhile.)"""
    if kind in ("planner", "free_plan"):
        r = conn.execute("SELECT status FROM campaigns WHERE id=?", (int(p.get("cid") or 0),)).fetchone()
        return bool(r and r["status"] == "planning")
    if kind == "competitors":
        r = conn.execute("SELECT status FROM competitor_reports WHERE id=?", (int(p.get("rid") or 0),)).fetchone()
        return bool(r and r["status"] == "running")
    return False


def run_task(kind: str, p: dict):
    """Run one task's work (in a worker thread)."""
    if kind == "planner":
        import planner
        return planner._safe_run(int(p["cid"]), p.get("notes"), _rebuild_hold(p.get("hold")))
    if kind == "competitors":
        import competitors
        if p.get("hold_id"):
            import credits
            credits._keep_alive(int(p["hold_id"]))
        return competitors._safe_run(int(p["rid"]))
    if kind == "free_plan":
        import paywall_routes
        return paywall_routes.run(int(p["claim"]), int(p["cid"]), int(p["rid"]) if p.get("rid") else None)
    raise ValueError(f"unknown task kind {kind}")


def claim(conn, tid: int, name: str) -> bool:
    """Take one queued task for `name` (compare-and-set: two workers never both win)."""
    t = db.now()
    cur = conn.execute("UPDATE task_queue SET status='running', claimed_by=?, claimed_at=?, lease_until=?, "
                       "attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'",
                       (name, t, _iso(LEASE_S), t, int(tid)))
    conn.commit()
    return cur.rowcount == 1


def _finish(tid: int, status: str, error: str | None = None):
    t = db.now()
    with db.connect() as conn:
        conn.execute("UPDATE task_queue SET status=?, error=?, lease_until=NULL, finished_at=?, updated_at=? WHERE id=? "
                     "AND status='running'", (status, (error or None) and str(error)[:300], t, t, int(tid)))
        conn.commit()


def _thread(tid: int, kind: str, p: dict):
    try:
        run_task(kind, p)
        _finish(tid, "done")
    except Exception as e:  # noqa: BLE001 — the run's own _safe_run already marked its row
        traceback.print_exc()
        _finish(tid, "failed", f"{type(e).__name__}: {e}")
    finally:
        with _lock:
            _running.pop(tid, None)


def renew(name: str) -> int:
    """Push out the lease of every task this process is running. Returns how many."""
    with _lock:
        ids = list(_running)
    if not ids:
        return 0
    with db.connect() as conn:
        n = conn.execute(f"UPDATE task_queue SET lease_until=?, updated_at=? WHERE status='running' AND claimed_by=? AND "
                         f"id IN ({','.join('?' * len(ids))})", (_iso(LEASE_S), db.now(), name, *ids)).rowcount
        conn.commit()
    return n


def _start_renewer(name: str):
    if _renewer[0] is not None and _renewer[0].is_alive():
        return

    def loop():
        while True:
            time.sleep(RENEW_S)
            try:
                renew(name)
            except Exception:  # noqa: BLE001
                log.exception("task lease renewal failed")
    _renewer[0] = threading.Thread(target=loop, name="rs-task-lease", daemon=True)
    _renewer[0].start()


def sweep(conn) -> int:
    """Running tasks whose lease ran out belong to a worker that died: mark them interrupted (never re-run; the run's
    row and its credit hold are settled by the stale checks). Finished rows older than KEEP_DAYS go."""
    n = conn.execute("UPDATE task_queue SET status='interrupted', error='The worker stopped while it ran.', "
                     "lease_until=NULL, finished_at=?, updated_at=? WHERE status='running' AND lease_until < ?",
                     (db.now(), db.now(), _iso())).rowcount
    conn.execute("DELETE FROM task_queue WHERE status NOT IN ('queued','running') AND COALESCE(finished_at, updated_at) < ?",
                 (_iso(-KEEP_DAYS * 86400),))
    conn.commit()
    if n:
        log.warning("%s task(s) interrupted: their worker stopped", n)
    return n


def tick(name: str | None = None, inline: bool = False) -> int:
    """The worker loop's call: sweep, then claim queued tasks up to the free threads and start them. `inline` runs
    them in this thread (tests). Returns how many were started. Never raises."""
    name = name or worker_name()
    started = 0
    try:
        with db.connect() as conn:
            sweep(conn)
            with _lock:
                free = threads() - len(_running)
            if free <= 0:
                return 0
            rows = conn.execute("SELECT * FROM task_queue WHERE status='queued' ORDER BY id LIMIT ?",
                                (free * 2,)).fetchall()
            for r in rows:
                if started >= free:
                    break
                p = json.loads(r["payload_json"] or "{}")
                if not still_wanted(conn, r["kind"], p):
                    conn.execute("UPDATE task_queue SET status='skipped', error='Its run had already stopped.', "
                                 "finished_at=?, updated_at=? WHERE id=? AND status='queued'", (db.now(), db.now(), r["id"]))
                    conn.commit()
                    continue
                if not claim(conn, r["id"], name):
                    continue
                started += 1
                if inline:
                    with _lock:
                        _running[r["id"]] = threading.current_thread()
                    _thread(r["id"], r["kind"], p)
                    continue
                th = threading.Thread(target=_thread, args=(r["id"], r["kind"], p), name=f"task-{r['kind']}-{r['id']}",
                                      daemon=True)
                with _lock:
                    _running[r["id"]] = th
                th.start()
        if not inline and started:
            _start_renewer(name)
    except Exception:  # noqa: BLE001
        log.exception("task tick failed")
    return started


def busy() -> int:
    with _lock:
        return len(_running)


def wait(timeout: float) -> int:
    """At shutdown: wait up to `timeout` seconds for running tasks. Returns how many are still running."""
    end = time.time() + timeout
    while time.time() < end:
        with _lock:
            left = [t for t in _running.values() if t.is_alive()]
        if not left:
            return 0
        time.sleep(0.5)
    with _lock:
        return len(_running)


def release(name: str) -> int:
    """At shutdown, after wait(): this worker's tasks still running are interrupted now (not when the lease lapses)."""
    with db.connect() as conn:
        n = conn.execute("UPDATE task_queue SET status='interrupted', error='The worker stopped while it ran.', "
                         "lease_until=NULL, finished_at=?, updated_at=? WHERE status='running' AND claimed_by=?",
                         (db.now(), db.now(), name)).rowcount
        conn.commit()
    return n


def counts(conn) -> dict:
    """For the System card: queued, running, and the last day's outcomes."""
    out = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM task_queue WHERE status IN ('queued','running') OR updated_at >= ? "
        "GROUP BY status", (_iso(-86400),))}
    old = conn.execute("SELECT MIN(created_at) t FROM task_queue WHERE status='queued'").fetchone()
    out["oldest_queued"] = old["t"] if old else None
    return out

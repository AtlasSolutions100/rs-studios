"""SQLite contention and size, measured per process (roadmap M7.3; mirrors Crowdcut's dbstats.py, its M4.5).

The web process (gunicorn) and the worker share one WAL database (RS_DB). This module measures how long writers
wait for it, so the move to Postgres happens on a number, not a feeling.

db.connect() opens every connection with TimedConnection (DB_STATS=off goes back to the plain sqlite3.Connection).
For each write transaction it records:

  wait  the time of its first write statement (the INSERT / UPDATE / DELETE / REPLACE / BEGIN IMMEDIATE that takes
        the database's write lock). sqlite's busy timeout (15 s, db.connect) sleeps inside that statement while
        another connection holds the lock, so this is the busy wait plus the statement itself (well under 5 ms on
        its own). A first write over BUSY_MS (20 ms) counts as one busy wait.
  hold  first write to commit or rollback: how long this connection held the lock.
  locked  "database is locked" errors (the 15 s timeout ran out).
  slow connects  db.connect() calls over 1 s (each logs one "DB SLOW" warning).

Counters live in memory. A daemon thread in each process writes one db_contention row a minute when there was
anything to record (histograms as JSON, so a p95 can be taken over any window), and deletes rows older than
DB_STATS_KEEP_DAYS (30). The admin page's System card (Database load) reads report(); the Postgres threshold
(write lock wait p95 over DB_PG_WAIT_P95_MS = 200 ms over the last 24 h with at least DB_PG_MIN_WRITES = 500
writes, or the database over DB_PG_SIZE_GB = 5 GB) shows there, and condition() gives alerts.sweep the operator
alert "postgres_threshold" at most once a day.
"""
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

BUCKETS_MS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000]   # then +inf
BUSY_MS = 20.0
SLOW_CONNECT_S = 1.0
FLUSH_S = 60.0
WRITE_HEADS = ("INSERT", "UPDATE", "DELETE", "REPLACE")

_lock = threading.Lock()
_flusher = {"pid": None}
_last_prune = {"at": 0.0}


def _num(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def enabled() -> bool:
    return (os.environ.get("DB_STATS") or "on").strip().lower() not in ("0", "off", "false", "no")


def _empty() -> dict:
    n = len(BUCKETS_MS) + 1
    return {"writes": 0, "busy": 0, "locked": 0, "slow_connects": 0, "wait_hist": [0] * n, "hold_hist": [0] * n,
            "wait_max_ms": 0.0, "hold_max_ms": 0.0, "wait_sum_ms": 0.0}


_stats = _empty()


def _bucket(ms: float) -> int:
    for i, b in enumerate(BUCKETS_MS):
        if ms <= b:
            return i
    return len(BUCKETS_MS)


def note_wait(ms: float, locked: bool = False):
    with _lock:
        _stats["writes"] += 1
        _stats["wait_hist"][_bucket(ms)] += 1
        _stats["wait_sum_ms"] += ms
        _stats["wait_max_ms"] = max(_stats["wait_max_ms"], ms)
        if ms > BUSY_MS:
            _stats["busy"] += 1
        if locked:
            _stats["locked"] += 1


def note_hold(ms: float):
    with _lock:
        _stats["hold_hist"][_bucket(ms)] += 1
        _stats["hold_max_ms"] = max(_stats["hold_max_ms"], ms)


def note_connect(seconds: float):
    if seconds > SLOW_CONNECT_S:
        with _lock:
            _stats["slow_connects"] += 1
        logging.getLogger("campaignify").warning("DB SLOW: connect took %.1f s (%s)", seconds, proc_label())


def _is_write(sql) -> bool:
    """A statement that takes the write lock: DML, or BEGIN IMMEDIATE / EXCLUSIVE
    (a plain BEGIN is deferred: its first DML takes the lock and is timed instead)."""
    try:
        head = sql.lstrip()[:16].upper()
    except AttributeError:
        return False
    if head.startswith("BEGIN"):
        return "IMMEDIATE" in head or "EXCLUSIVE" in head
    return head.startswith(WRITE_HEADS)


class TimedConnection(sqlite3.Connection):
    """sqlite3.Connection that times the first write of each transaction and the
    time to its commit. Everything else is the stock connection."""

    _w0 = None

    def _timed(self, fn, sql, *args):
        if self._w0 is not None or not _is_write(sql):
            return fn(sql, *args)
        t0 = time.perf_counter()
        try:
            out = fn(sql, *args)
        except sqlite3.OperationalError as e:
            note_wait((time.perf_counter() - t0) * 1000, locked="locked" in str(e).lower())
            raise
        note_wait((time.perf_counter() - t0) * 1000)
        if self.in_transaction:
            self._w0 = t0
        return out

    def execute(self, sql, *args):
        return self._timed(super().execute, sql, *args)

    def executemany(self, sql, *args):
        return self._timed(super().executemany, sql, *args)

    def _held(self):
        if self._w0 is not None:
            note_hold((time.perf_counter() - self._w0) * 1000)
            self._w0 = None

    def commit(self):
        try:
            return super().commit()
        finally:
            self._held()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._held()

    def close(self):
        try:
            return super().close()
        finally:
            self._w0 = None

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self._held()


class PlainConnection(sqlite3.Connection):
    """DB_STATS=off: the stock connection (a subclass only so db.log_batch can mark it)."""


def factory():
    """The connection class db.connect() uses (and starts this process's flusher)."""
    if not enabled():
        return PlainConnection
    if _flusher["pid"] != os.getpid():
        _flusher["pid"] = os.getpid()
        threading.Thread(target=_flush_loop, name="dbstats", daemon=True).start()
    return TimedConnection


def proc_label() -> str:
    """web (gunicorn), worker (the worker process, RS_PROC_ROLE=worker), or the script's name."""
    role = (os.environ.get("RS_PROC_ROLE") or "").strip().lower()
    if role:
        return role
    argv = [os.path.basename(a) for a in (sys.argv or [""])]
    if argv and "gunicorn" in argv[0]:
        return "web"
    name = argv[0] if argv else ""
    if name.endswith(".py"):
        return name[:-3]
    return name or "python"


# ---------------------------------------------------------------- the table

def _db_path() -> str:
    import db
    return db.DB_PATH


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS db_contention (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        proc          TEXT NOT NULL,
        pid           INTEGER,
        writes        INTEGER NOT NULL DEFAULT 0,
        busy          INTEGER NOT NULL DEFAULT 0,
        locked        INTEGER NOT NULL DEFAULT 0,
        slow_connects INTEGER NOT NULL DEFAULT 0,
        wait_hist     TEXT,
        hold_hist     TEXT,
        wait_max_ms   REAL,
        hold_max_ms   REAL,
        wait_sum_ms   REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_db_contention_ts ON db_contention(ts)")


def take() -> dict:
    """This process's counters since the last take (and reset them)."""
    global _stats
    with _lock:
        out, _stats = _stats, _empty()
    return out


def _merge_back(s: dict):
    with _lock:
        for k in ("writes", "busy", "locked", "slow_connects", "wait_sum_ms"):
            _stats[k] += s[k]
        for k in ("wait_hist", "hold_hist"):
            _stats[k] = [a + b for a, b in zip(_stats[k], s[k])]
        for k in ("wait_max_ms", "hold_max_ms"):
            _stats[k] = max(_stats[k], s[k])


def flush(path: str | None = None, now: datetime | None = None) -> bool:
    """Write this process's counters as one row (nothing when there were none).
    A plain connection, so the flush is not measured itself. Never raises."""
    s = take()
    if not (s["writes"] or s["slow_connects"] or s["locked"]):
        return False
    now = now or datetime.now(timezone.utc)
    try:
        conn = sqlite3.connect(path or _db_path(), timeout=5)
        try:
            ensure_table(conn)
            conn.execute("INSERT INTO db_contention (ts, proc, pid, writes, busy, locked, slow_connects, wait_hist, "
                         "hold_hist, wait_max_ms, hold_max_ms, wait_sum_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (now.isoformat(timespec="seconds"), proc_label(), os.getpid(), s["writes"], s["busy"],
                          s["locked"], s["slow_connects"], json.dumps(s["wait_hist"]), json.dumps(s["hold_hist"]),
                          round(s["wait_max_ms"], 2), round(s["hold_max_ms"], 2), round(s["wait_sum_ms"], 2)))
            if time.time() - _last_prune["at"] > 3600:
                _last_prune["at"] = time.time()
                keep = _num("DB_STATS_KEEP_DAYS", 30)
                conn.execute("DELETE FROM db_contention WHERE ts < ?",
                             ((now - timedelta(days=keep)).isoformat(timespec="seconds"),))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:  # noqa: BLE001 — stats never break the app
        _merge_back(s)
        logging.getLogger("campaignify").warning("dbstats: flush skipped (%s)", str(e)[:80])
        return False


def _flush_loop():
    while True:
        time.sleep(FLUSH_S)
        if _flusher["pid"] != os.getpid():
            return              # a forked child runs its own
        flush()


# ---------------------------------------------------------------- reading it back

def p_from_hist(hist: list, q: float = 0.95) -> float | None:
    """The q-quantile of a histogram, as its bucket's upper bound in ms (conservative)."""
    total = sum(hist or [])
    if not total:
        return None
    need = q * total
    run = 0
    for i, n in enumerate(hist):
        run += n
        if run >= need:
            return float(BUCKETS_MS[i]) if i < len(BUCKETS_MS) else float(BUCKETS_MS[-1] * 2)
    return float(BUCKETS_MS[-1] * 2)


def _add(a: list, b: list) -> list:
    if not a:
        return list(b)
    return [x + y for x, y in zip(a, b)]


def db_bytes(path: str | None = None) -> int:
    p = path or _db_path()
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(p + suffix)
        except OSError:
            pass
    return total


def thresholds() -> dict:
    return {"wait_p95_ms": _num("DB_PG_WAIT_P95_MS", 200), "size_gb": _num("DB_PG_SIZE_GB", 5),
            "min_writes": int(_num("DB_PG_MIN_WRITES", 500))}


def report(conn, hours: int = 48, now: datetime | None = None) -> dict:
    """Hourly series for the System page, the last 24 h by process, and the threshold."""
    ensure_table(conn)
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    day = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT * FROM db_contention WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    hourly, procs, last = {}, {}, {"writes": 0, "busy": 0, "locked": 0, "slow_connects": 0, "wait": [], "hold": []}
    for r in rows:
        wait, hold = json.loads(r["wait_hist"] or "[]"), json.loads(r["hold_hist"] or "[]")
        h = r["ts"][:13]
        e = hourly.setdefault(h, {"hour": h + ":00", "writes": 0, "busy": 0, "locked": 0, "wait": [], "max": 0.0})
        e["writes"] += r["writes"]
        e["busy"] += r["busy"]
        e["locked"] += r["locked"]
        e["wait"] = _add(e["wait"], wait)
        e["max"] = max(e["max"], r["wait_max_ms"] or 0)
        if r["ts"] >= day:
            role = r["proc"]
            p = procs.setdefault(role, {"proc": role, "writes": 0, "busy": 0, "locked": 0, "wait": [], "hold": [],
                                        "max": 0.0})
            for k in ("writes", "busy", "locked"):
                p[k] += r[k]
                last[k] += r[k]
            last["slow_connects"] += r["slow_connects"]
            p["wait"], p["hold"] = _add(p["wait"], wait), _add(p["hold"], hold)
            last["wait"], last["hold"] = _add(last["wait"], wait), _add(last["hold"], hold)
            p["max"] = max(p["max"], r["wait_max_ms"] or 0)
    series = [{"hour": e["hour"], "writes": e["writes"], "busy": e["busy"], "locked": e["locked"],
               "wait_p95_ms": p_from_hist(e["wait"]), "wait_max_ms": round(e["max"], 1)}
              for e in sorted(hourly.values(), key=lambda x: x["hour"])]
    by_proc = [{"proc": p["proc"], "writes": p["writes"], "busy": p["busy"], "locked": p["locked"],
                "wait_p95_ms": p_from_hist(p["wait"]), "hold_p95_ms": p_from_hist(p["hold"]),
                "wait_max_ms": round(p["max"], 1)} for p in sorted(procs.values(), key=lambda x: -x["writes"])]
    th = thresholds()
    p95 = p_from_hist(last["wait"])
    size = db_bytes()
    over_wait = bool(p95 is not None and last["writes"] >= th["min_writes"] and p95 > th["wait_p95_ms"])
    over_size = size > th["size_gb"] * 1024 ** 3
    return {"on": enabled(), "hours": hours, "series": series, "by_proc": by_proc,
            "day": {"writes": last["writes"], "busy": last["busy"], "locked": last["locked"],
                    "slow_connects": last["slow_connects"], "wait_p95_ms": p95,
                    "hold_p95_ms": p_from_hist(last["hold"])},
            "db_bytes": size, "threshold": th, "over_wait": over_wait, "over_size": over_size,
            "tripped": over_wait or over_size}


ALERT_META = "dbstats_alert_at"     # when the postgres_threshold alert last went out (once a day at most)
_last_check = {"at": 0.0, "hit": None}


def _why(rep: dict) -> str:
    th, d = rep["threshold"], rep["day"]
    why = []
    if rep["over_wait"]:
        why.append(f"write lock wait p95 {d['wait_p95_ms']:g} ms over 24 h (threshold {th['wait_p95_ms']:g} ms, "
                   f"{d['writes']} writes, {d['busy']} busy waits, {d['locked']} lock timeouts)")
    if rep["over_size"]:
        why.append(f"database {rep['db_bytes'] / 1024 ** 3:.1f} GB (threshold {th['size_gb']:g} GB)")
    return "; ".join(why)


def condition(conn, force: bool = False):
    """For alerts.sweep (every minute): ("postgres_threshold", title, detail) when the threshold has tripped and
    the alert hasn't gone out in the last 24 hours, else None. Looks at the numbers once an hour. Never raises."""
    if not force and time.time() - _last_check["at"] < 3600:
        return None
    _last_check["at"] = time.time()
    try:
        rep = report(conn, hours=24)
    except Exception:  # noqa: BLE001
        return None
    _last_check["hit"] = rep["tripped"]
    if not rep["tripped"]:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (ALERT_META,)).fetchone()
        if row and time.time() - float(row["value"] or 0) < 24 * 3600 - 60:
            return None
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (ALERT_META, str(time.time())))
        conn.commit()
    except Exception:  # noqa: BLE001
        return None
    return ("postgres_threshold", "SQLite has reached the Postgres threshold",
            _why(rep) + ". Plan the move to Postgres. Admin > System > Database load.")

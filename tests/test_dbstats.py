"""SQLite contention and size metrics (dbstats.py, roadmap M7.3; Crowdcut's M4.5 meter).

    python tests/test_dbstats.py      (tests/test_app.py runs it too)

  * every db.connect() connection times its first write (the lock wait) and its hold; a write that waits behind
    another connection's lock counts as a busy wait, a timeout as locked
  * one row a minute per process (flush), the p95 from the histograms, 30 days kept
  * the Postgres threshold: wait p95 over 200 ms for 24 h with at least 500 writes, or the database over 5 GB;
    the operator alert postgres_threshold at most once a day; the admin System card's Database load row
  * DB_STATS=off: the plain connection
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_dbstats_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", OPS_ALERTS="1")
for k in ("DB_STATS", "DB_PG_WAIT_P95_MS", "DB_PG_SIZE_GB", "DB_PG_MIN_WRITES", "OPS_ALERT_EMAIL", "RESEND_API_KEY"):
    os.environ.pop(k, None)

import alerts  # noqa: E402
import app as appmod  # noqa: E402
import db  # noqa: E402
import dbstats  # noqa: E402

alerts.ASYNC = False
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        shutil.rmtree(TMP, True)
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


# ---------------------------------------------------------------- measuring
print("measuring")
dbstats.take()
with db.connect() as conn:
    check(isinstance(conn, dbstats.TimedConnection), "db.connect gives the timed connection")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dbstats_test', '1')")
    conn.commit()
    conn.execute("SELECT * FROM meta").fetchall()
s = dbstats.take()
check(s["writes"] == 1 and sum(s["wait_hist"]) == 1 and sum(s["hold_hist"]) == 1 and s["busy"] == 0,
      "a write counts once with its wait and its hold; reads don't count")

holder = sqlite3.connect(db.DB_PATH, timeout=15, isolation_level=None, check_same_thread=False)
holder.execute("BEGIN IMMEDIATE")
release = threading.Timer(0.25, lambda: holder.execute("COMMIT"))
release.start()
t0 = time.perf_counter()
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dbstats_test', '2')")
    conn.commit()
waited = (time.perf_counter() - t0) * 1000
release.join()
holder.close()
s = dbstats.take()
check(waited >= 200 and s["busy"] == 1 and s["wait_max_ms"] >= 200,
      f"a write that waits behind another connection's lock is a busy wait ({s['wait_max_ms']:.0f} ms)")

holder = sqlite3.connect(db.DB_PATH, timeout=15, isolation_level=None)
holder.execute("BEGIN IMMEDIATE")
c = db.connect()
c.execute("PRAGMA busy_timeout=100")
try:
    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dbstats_test', '3')")
    check(False, "the write should time out")
except sqlite3.OperationalError as e:
    check("locked" in str(e), "the lock timeout is raised as before")
c.close()
holder.execute("COMMIT")
holder.close()
s = dbstats.take()
check(s["locked"] == 1, "and counted as a lock timeout")

# ---------------------------------------------------------------- flush and report
print("flush and report")
with db.connect() as conn:
    for i in range(3):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')", (f"dbstats_{i}",))
        conn.commit()
check(dbstats.flush(), "a minute's counters go in one row")
check(not dbstats.flush(), "nothing to record: no row")
with db.connect() as conn:
    rows = conn.execute("SELECT * FROM db_contention").fetchall()
    rep = dbstats.report(conn)
check(len(rows) == 1 and rows[0]["writes"] == 3 and rows[0]["proc"], "the row has the writes and the process")
check(rep["day"]["writes"] == 3 and rep["day"]["wait_p95_ms"] is not None and not rep["tripped"] and rep["db_bytes"] > 0,
      "the report: writes, p95, size, under the threshold")
check(dbstats.p_from_hist([0, 0, 0, 0, 0, 0, 0, 0, 10] + [0] * 6) == 500.0, "p95 from a histogram is its bucket's top")
os.environ["RS_PROC_ROLE"] = "worker"
check(dbstats.proc_label() == "worker", "the worker process says so (RS_PROC_ROLE)")
os.environ.pop("RS_PROC_ROLE")

# ---------------------------------------------------------------- the threshold and the alert
print("the Postgres threshold")
n = len(dbstats.BUCKETS_MS) + 1
slow = [0] * n
slow[dbstats.BUCKETS_MS.index(500)] = 600        # 600 writes that waited up to 500 ms
now = datetime.now(timezone.utc)
with db.connect() as conn:
    conn.execute("INSERT INTO db_contention (ts, proc, pid, writes, busy, locked, slow_connects, wait_hist, hold_hist, "
                 "wait_max_ms, hold_max_ms, wait_sum_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ((now - timedelta(hours=2)).isoformat(timespec="seconds"), "web", 1, 600, 600, 2, 0, json.dumps(slow),
                  json.dumps(slow), 480.0, 480.0, 600 * 300.0))
    conn.execute("INSERT INTO db_contention (ts, proc, pid, writes, wait_hist, hold_hist) VALUES (?,?,?,?,?,?)",
                 ((now - timedelta(days=40)).isoformat(timespec="seconds"), "old-web", 1, 9, json.dumps(slow), json.dumps(slow)))
    conn.commit()
    rep = dbstats.report(conn)
check(rep["over_wait"] and rep["tripped"] and not rep["over_size"] and rep["day"]["wait_p95_ms"] == 500.0,
      "wait p95 over 200 ms with 500+ writes trips the threshold")
check(rep["by_proc"][0]["proc"] == "web" and rep["series"], "by process and by hour")
fired = alerts.sweep(probe=False)
check("postgres_threshold" in fired, "the operator alert fires")
dbstats._last_check["at"] = 0
check("postgres_threshold" not in alerts.sweep(probe=False), "and not again the same day")
with db.connect() as conn:
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(time.time() - 25 * 3600), dbstats.ALERT_META))
    conn.commit()
    check(dbstats.condition(conn, force=True) is not None, "a day later it can go out again")
    conn.execute("DELETE FROM db_contention WHERE writes=600")
    conn.commit()
    os.environ["DB_PG_SIZE_GB"] = "0.000001"
    rep = dbstats.report(conn)
    check(rep["over_size"] and rep["tripped"] and not rep["over_wait"], "a database over the size threshold trips it too")
    os.environ.pop("DB_PG_SIZE_GB")
    check(not dbstats.report(conn)["tripped"], "and under both it is clear")
dbstats._last_prune["at"] = 0
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dbstats_prune', '1')")
    conn.commit()
flushed = dbstats.flush()
with db.connect() as conn:
    old = conn.execute("SELECT ts FROM db_contention WHERE proc='old-web'").fetchall()
    check(flushed and not old, "rows over 30 days old are removed")

# ---------------------------------------------------------------- the admin card
print("the admin System card")
cl = appmod.app.test_client()
cl.get("/login")
with cl.session_transaction() as sess:
    tok = sess["csrf"]
cl.post("/signup", data={"csrf": tok, "email": "dbstats-admin@example.com", "password": "longpassword"})
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1, email_verified_at=? WHERE email='dbstats-admin@example.com'", (db.now(),))
    conn.commit()
html = cl.get("/admin").get_data(as_text=True)
check('id="dbload"' in html and "Database load" in html and "under the Postgres threshold" in html
      and "lock wait p95" in html, "Database load on the System card, with the threshold")

# ---------------------------------------------------------------- off
os.environ["DB_STATS"] = "off"
with db.connect() as conn:
    check(not isinstance(conn, dbstats.TimedConnection), "DB_STATS=off: the plain connection")
os.environ.pop("DB_STATS")

shutil.rmtree(TMP, True)
print(f"\nall {passed} checks passed")

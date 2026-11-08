"""Operator alerts and the real /health check (alerts.py, opslog.py; roadmap M1.7),
on a throwaway database with the mail API and the vendor faked.

What it pins down:
  * /health reads and writes the database and checks the worker heartbeat: a
    heartbeat over 180 s old, a dead worker thread or a broken database gives 503;
    a long vendor call keeps beating (busy()), and a new process is judged by its
    own worker thread, not the old container's last beat (N2);
  * each alert condition fires (worker down, stuck jobs, webhook signature
    failures, vendor errors, disk low, backup late, refund and dispute) and only
    then; one email per key per hour, repeats counted into the next one;
  * the address comes only from OPS_ALERT_EMAIL; nothing is sent without it;
  * logs are JSON with the request id and workspace id, and never the real path.

    python tests/test_alerts.py      (tests/test_app.py runs it too)
"""
import io
import json
import logging
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_alerts_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.example.com", OPS_ALERT_EMAIL="ops@example.com")
for k in ("OPS_ALERTS", "OPS_ALERT_DEDUPE_MIN", "RS_LOG_JSON", "RS_ACCESS_LOG"):
    os.environ.pop(k, None)

import alerts  # noqa: E402
import app as appmod  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import mailer  # noqa: E402
import opslog  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


alerts.ASYNC = False
sent = []


class _R:
    status_code = 200

    def json(self):
        return {"id": "em_1"}


def _fake_post(url, json=None, headers=None, timeout=None):
    sent.append({"url": url, "body": json, "headers": headers})
    return _R()


mailer.requests.post = _fake_post


def reset():
    sent.clear()
    with db.connect() as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'alert:%'")
        conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (alerts.HEARTBEAT_META, alerts.BACKUP_META))
        conn.execute("DELETE FROM throttle WHERE key LIKE 'ops:%'")
        conn.execute("DELETE FROM jobs")
        conn.commit()
    alerts._beat.update(at=0.0, saved=0.0)


def subjects():
    return [m["body"]["subject"] for m in sent]


def iso(minutes_ago=0.0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()


def add_job(status, minutes_ago, finished_minutes_ago=None, tool="ad_image"):
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at, finished_at) "
                     "VALUES (1, 1, ?, ?, ?, ?, ?)",
                     (tool, status, iso(minutes_ago), iso(minutes_ago),
                      iso(finished_minutes_ago) if finished_minutes_ago is not None else None))
        conn.commit()


def set_beat(seconds_ago):
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (alerts.HEARTBEAT_META, str(time.time() - seconds_ago)))
        conn.commit()


c = appmod.app.test_client()

print("== /health")
reset()
r = c.get("/health")
j = r.get_json()
check(r.status_code == 200 and j["ok"] is True, "healthy: 200 and ok")
check(j["checks"]["db"] == "ok" and j["checks"]["worker"]["status"] == "off",
      "db read/write checked; no worker in this process and no heartbeat reads as off")
check(j["generation_configured"] is True and j["checks"]["vendor"] in ("unknown", "ok"),
      "still reports generation_configured; vendor from the cached probe")
with db.connect() as conn:
    check(conn.execute("SELECT value FROM meta WHERE key='health_probe'").fetchone() is not None,
          "the check writes to the database")

set_beat(5)
r = c.get("/health")
check(r.status_code == 200 and r.get_json()["checks"]["worker"]["status"] == "ok", "fresh heartbeat: 200")

set_beat(90)
r = c.get("/health")
check(r.status_code == 200 and r.get_json()["checks"]["worker"]["status"] == "ok",
      "a heartbeat 90 s old (one slow vendor call) is still ok: the limit is 180 s (N2)")

set_beat(200)
r = c.get("/health")
check(r.status_code == 503 and r.get_json()["checks"]["worker"]["status"] == "late",
      "heartbeat older than 180 s: 503 late")
check(not sent, "a late worker under 5 minutes doesn't email yet")

set_beat(400)
r = c.get("/health")
check(r.status_code == 503, "worker gone 400 s: 503")
check(len(sent) == 1 and "worker isn't running" in subjects()[0], "worker gone over 5 minutes: alert emailed")
m = sent[0]
check(m["body"]["to"] == ["ops@example.com"] and m["body"]["from"] == '"Campaignify alerts" <alerts@mail.example.com>',
      "sent to OPS_ALERT_EMAIL from the shared mail domain")
c.get("/health")
check(len(sent) == 1, "same alert again within the hour: not emailed twice")


class DeadThread:
    def is_alive(self):
        return False


reset()
worker._thread = DeadThread()
try:
    r = c.get("/health")
    check(r.status_code == 503 and r.get_json()["checks"]["worker"]["status"] == "stopped",
          "worker thread died: 503 stopped")
    check(any("worker isn't running" in s for s in subjects()), "a dead worker thread alerts at once")
finally:
    worker._thread = None

print("== heartbeat during long calls; a new container isn't judged by the old one's beat (N2)")


class LiveThread:
    def is_alive(self):
        return True


reset()
set_beat(3000)   # the previous container's last beat, left in the shared database
_real_beat_at, _real_started = alerts._beat["at"], alerts.STARTED
alerts._beat["at"] = 0.0
alerts.STARTED = time.time()
worker._thread = LiveThread()
try:
    r = c.get("/health")
    check(r.status_code == 200 and r.get_json()["checks"]["worker"]["status"] == "starting",
          "a just-started process with a live worker thread and a stale beat in the database: 200 starting")
    alerts.STARTED = time.time() - 170
    r = c.get("/health")
    check(r.status_code == 200, "still starting 170 s in (the deploy hook waits 120 s after the switch)")
    alerts.STARTED = time.time() - 400
    r = c.get("/health")
    check(r.status_code == 503 and r.get_json()["checks"]["worker"]["status"] == "no heartbeat",
          "a live thread that never beat after 400 s: 503")
    alerts._beat["at"] = time.time() - 200
    r = c.get("/health")
    check(r.status_code == 503 and r.get_json()["checks"]["worker"]["status"] == "late",
          "its own beat 200 s old: late (a hung loop still fails)")
    alerts._beat["at"] = time.time() - 5
    set_beat(3000)
    r = c.get("/health")
    check(r.status_code == 200, "a process with its own worker goes by its own beats, not the database's")
finally:
    worker._thread = None
    alerts._beat["at"], alerts.STARTED = _real_beat_at, _real_started

import threading  # noqa: E402

reset()
alerts._beat["at"] = time.time() - 170
worker._thread = threading.current_thread()   # pretend this thread is the worker loop
try:
    with alerts.busy("vendor submit"):
        check(alerts._busy["what"] == "vendor submit" and alerts._ticker[0] is not None,
              "busy() marks the call and starts the side ticker")
        alerts._beat["at"] = time.time() - 170
        check(alerts.busy_tick() and time.time() - alerts._beat["at"] < 2, "the ticker beats while the call runs")
        state = alerts.worker_state(db.connect())
        check(state["ok"] and state["busy"] == "vendor submit" and state["busy_s"] >= 0,
              "/health says which call is in progress and for how long")
        alerts._busy["since"] = time.time() - 1000
        alerts._beat["at"] = time.time() - 300
        check(not alerts.busy_tick() and time.time() - alerts._beat["at"] > 200,
              "past RS_HEALTH_BUSY_MAX_S (900 s) the ticker stops, so a stuck call shows as late")
    check(alerts._busy["depth"] == 0 and not alerts._busy["since"] and time.time() - alerts._beat["at"] < 2,
          "leaving busy() beats once and clears the mark")
finally:
    worker._thread = None
with alerts.busy("web request"):
    check(alerts._busy["depth"] == 0, "outside the worker thread busy() does nothing")

reset()
_real_connect = alerts.db.connect


def _broken():
    raise RuntimeError("disk I/O error")


alerts.db.connect = _broken
try:
    r = alerts.health()
finally:
    alerts.db.connect = _real_connect
check(r[1] == 503 and r[0]["checks"]["db"] == "error", "database can't be used: 503")

print("== sweep conditions")
_probe_result = {"v": "ok"}
_real_probe = hf.probe
hf.probe = lambda max_age=600: _probe_result["v"]
_free = {"gb": 50.0}
_real_disk = alerts.disk_free_gb
alerts.disk_free_gb = lambda path=None: _free["gb"]

reset()
add_job("done", 60, 59)
add_job("running", 10)
check(alerts.sweep() == [], "nothing wrong: no alert")

reset()
add_job("running", 25)
add_job("pending", 30)
fired = alerts.sweep()
check(fired == ["jobs_stuck"] and "2 job(s) stuck over 20 minutes" in subjects()[0], "jobs over 20 minutes: alert")
check(alerts.sweep() == [] and len(sent) == 1, "stuck again a minute later: deduped")
with db.connect() as conn:
    st = json.loads(conn.execute("SELECT value FROM meta WHERE key='alert:jobs_stuck'").fetchone()["value"])
    st["last_at"] = time.time() - 3601
    conn.execute("UPDATE meta SET value=? WHERE key='alert:jobs_stuck'", (json.dumps(st),))
    conn.commit()
check(alerts.sweep() == ["jobs_stuck"] and "1 more time" in sent[-1]["body"]["text"],
      "after an hour it emails again, with the held-back count")

reset()
for _ in range(4):
    add_job("failed", 10, 5)
add_job("done", 10, 5)
check(alerts.sweep() == ["vendor_errors"] and "4 of 5 jobs failed" in subjects()[0], "80% failed in 15 min: alert")
reset()
add_job("failed", 10, 5)
for _ in range(5):
    add_job("done", 10, 5)
check(alerts.sweep() == [], "1 of 6 failed (under 20%): no alert")
reset()
for _ in range(3):
    add_job("failed", 10, 5)
check(alerts.sweep() == [], "fewer than 5 jobs ended: no alert")
reset()
for _ in range(5):
    add_job("failed", 60, 40)
check(alerts.sweep() == [], "failures older than 15 minutes don't count")

reset()
_probe_result["v"] = "out_of_credit"
check(alerts.sweep() == ["vendor_out_of_credit"], "vendor says out of credit: alert")
_probe_result["v"] = "unreachable"
reset()
check(alerts.sweep() == [], "one unreachable probe alone: no alert (the error rate covers outages)")
_probe_result["v"] = "ok"

reset()
_free["gb"] = 7.5
check(alerts.sweep() == ["disk_low"] and "7.5 GB free" in subjects()[0], "disk under 10 GB: alert")
r = c.get("/health")
check(r.status_code == 200 and r.get_json()["checks"]["disk"] == "low", "low disk shows on /health without a 503")
_free["gb"] = 50.0

reset()
check(alerts.sweep() == [], "no backup recorded yet: quiet")
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (alerts.BACKUP_META, iso(27 * 60)))
    conn.commit()
check(alerts.sweep() == ["backup_late"], "last backup 27 hours ago: alert")
reset()
with db.connect() as conn:
    alerts.backup_ok(conn)
check(alerts.sweep() == [], "fresh backup: quiet")

print("== webhooks")
reset()


def bad_stripe():
    return c.post("/webhooks/stripe", data=b'{"type":"x"}', headers={"Stripe-Signature": "t=1,v1=bad"},
                  content_type="application/json")


for i in range(10):
    check(bad_stripe().status_code == 400, f"bad signature {i + 1} rejected") if i in (0, 9) else bad_stripe()
check(not sent, "10 signature failures in an hour: no alert yet")
bad_stripe()
check(len(sent) == 1 and "webhook signature failures" in subjects()[0], "the 11th in an hour: alert")
for _ in range(3):
    c.post("/webhooks/resend", data=b"{}", headers={"svix-id": "x", "svix-timestamp": "1", "svix-signature": "v1,x"})
check(len(sent) == 1, "more failures, other providers too: still one email this hour")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM throttle WHERE key='ops:webhook_sig'").fetchone()[0] == 14,
          "resend rejections count too")

reset()
with db.connect() as conn:
    wsid = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES ('Shop', 0, ?)", (db.now(),)).lastrowid
    conn.execute("INSERT INTO purchases (workspace_id, pack, credits, amount_cents, stripe_session_id, "
                 "stripe_payment_intent, status, created_at) VALUES (?, 'pro', 8500, 7900, 'cs_1', 'pi_ours', 'paid', ?)",
                 (wsid, db.now()))
    conn.commit()


def stripe_event(obj, typ):
    payload = json.dumps({"id": f"evt_{typ}_{obj['id']}", "object": "event", "data": {"object": obj},
                          "type": typ}).encode()
    return c.post("/webhooks/stripe", data=payload, headers={"Stripe-Signature": billing.sign(payload, "whsec_x")},
                  content_type="application/json")


r = stripe_event({"id": "ch_1", "object": "charge", "payment_intent": "pi_ours", "amount_refunded": 7900,
                  "currency": "usd"}, "charge.refunded")
check(r.status_code == 200, "refund event accepted")
check(len(sent) == 1 and subjects()[0] == f"[Campaignify] Refund: 79.00 USD, workspace {wsid}",
      "refund on our payment: alert with amount and workspace id")
stripe_event({"id": "ch_1", "object": "charge", "payment_intent": "pi_ours", "amount_refunded": 7900,
              "currency": "usd"}, "charge.refunded")
check(len(sent) == 1, "the same event delivered twice: one email")
stripe_event({"id": "dp_1", "object": "dispute", "payment_intent": "pi_ours", "amount": 7900, "currency": "usd",
              "status": "needs_response", "reason": "fraudulent"}, "charge.dispute.created")
check(len(sent) == 2 and "Dispute opened: 79.00 USD" in subjects()[1] and "fraudulent" in sent[1]["body"]["text"],
      "dispute on our payment: alert with the reason")
stripe_event({"id": "ch_9", "object": "charge", "payment_intent": "pi_someone_else", "amount_refunded": 500,
              "currency": "usd"}, "charge.refunded")
check(len(sent) == 2, "a refund for another app on the shared Stripe account: ignored")
stripe_event({"id": "ch_8", "object": "charge", "metadata": {"app": billing.APP_TAG, "workspace_id": str(wsid)},
              "amount_refunded": 2900, "currency": "usd"}, "charge.refunded")
check(len(sent) == 3 and f"workspace {wsid}" in subjects()[2], "a refund matched by our metadata: alert")
check(all("@" not in s.split("]")[1] for s in subjects()), "no email addresses in alert subjects")

print("== the address comes only from the setting")
reset()
os.environ["OPS_ALERT_EMAIL"] = ""
add_job("running", 25)
check(alerts.sweep() == ["jobs_stuck"] and not sent, "no OPS_ALERT_EMAIL: logged and recorded, nothing emailed")
os.environ["OPS_ALERT_EMAIL"] = "a@example.com, b@example.com"
check(alerts.recipients() == ["a@example.com", "b@example.com"], "several addresses, comma separated")
os.environ["OPS_ALERT_EMAIL"] = "ops@example.com"
src = open(os.path.join(os.path.dirname(HERE), "alerts.py")).read()
check(not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", src.replace("alerts@{mailer.domain()}", "")),
      "alerts.py hard-codes no email address")
reset()
os.environ["OPS_ALERTS"] = "0"
add_job("running", 25)
check(alerts.sweep() == [] and not sent, "OPS_ALERTS=0 switches alerts off")
os.environ.pop("OPS_ALERTS")


def _mail_down(*a, **k):
    raise mailer.requests.RequestException("down")


reset()
mailer.requests.post = _mail_down
add_job("running", 25)
check(alerts.sweep() == ["jobs_stuck"], "the mail API failing never breaks the sweep")
mailer.requests.post = _fake_post

hf.probe = _real_probe
alerts.disk_free_gb = _real_disk

print("== structured logs")
buf = io.StringIO()
h = logging.StreamHandler(buf)
h.setFormatter(opslog.JsonFormatter())
lg = logging.getLogger("campaignify")
lg.addHandler(h)
try:
    r = c.get("/u/secret-token-abc123?x=y", headers={"X-Request-Id": "req-12345678"})
    check(r.headers.get("X-Request-Id") == "req-12345678", "a sane X-Request-Id is echoed")
    r2 = c.get("/pricing", headers={"X-Request-Id": "bad id <script>"})
    rid = r2.headers.get("X-Request-Id") or ""
    check(re.fullmatch(r"[0-9a-f]{16}", rid), "an odd one is replaced with our own")
    lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
    reqs = [x for x in lines if x.get("msg") == "request"]
    check(any(x.get("request_id") == "req-12345678" and x.get("status") for x in reqs),
          "one JSON line per request with the request id and status")
    check("secret-token-abc123" not in buf.getvalue() and "x=y" not in buf.getvalue(),
          "the real path and query (tokens) never reach the log")
    check(any(x.get("route") == "/u/<token>" for x in reqs), "the route pattern is logged instead")
    with appmod.app.test_request_context("/"):
        from flask import g
        g.request_id, g.ws = "rid-abcdefgh", {"id": 42}
        lg.warning("something happened")
    last = json.loads(buf.getvalue().splitlines()[-1])
    check(last["request_id"] == "rid-abcdefgh" and last["ws"] == 42 and last["level"] == "warning",
          "app log lines carry request id and workspace id")
finally:
    lg.removeHandler(h)

print(f"\n{passed} checks passed")

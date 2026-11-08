"""Operator alerts and the real /health check (roadmap M1.7).

Alerts go by email through the same Resend path as member mail (mailer.py) to
OPS_ALERT_EMAIL (one address, or several separated by commas). Koby sets it;
nothing here hard-codes an address. Without it, or without RESEND_API_KEY and
RS_MAIL_DOMAIN, an alert is still logged at ERROR level and recorded, it just
isn't emailed.

Each alert has a key and is sent at most once per key per OPS_ALERT_DEDUPE_MIN
minutes (default 60); repeats inside the window are counted and the count goes
out with the next email. Dedupe state lives in the `meta` table, so there is no
schema change.

What fires an alert:
  worker_down          the worker loop hasn't run for OPS_WORKER_DOWN_MIN (5) minutes (seen by /health)
  jobs_stuck           a job has been pending or running for over OPS_JOB_STUCK_MIN (20) minutes
  webhook_signatures   more than OPS_WEBHOOK_SIG_PER_HOUR (10) webhook signature failures in an hour
  vendor_errors        over OPS_VENDOR_ERROR_PCT (20) % of jobs that ended in the last 15 minutes failed
                       (at least OPS_VENDOR_MIN_JOBS, 5, ended), or the vendor key is rejected / out of credit
  disk_low             free disk under OPS_ALERT_DISK_GB (10) GB where the data lives
  backup_late          the last good backup (meta BACKUP_META, written by the backup job) is over
                       OPS_BACKUP_MAX_HOURS (26) hours old; quiet until a first backup is recorded
  payment_reversed     a refund or dispute on one of our payments (charge.refunded, charge.dispute.*)

`sweep()` runs from the worker loop about once a minute; the worker-down check
runs in `/health` (the container healthcheck calls it every 30 s), because a
dead worker can't report itself.

The worker heartbeat (N2): the loop beats each pass and between jobs, and while
one long call runs (a vendor submit with its retries, a status poll, a 300 s
upload, a download) `busy()` keeps beating from a side thread every
BUSY_TICK_S, for up to RS_HEALTH_BUSY_MAX_S (900 s, longer than any one call
with its retries). /health fails on the heartbeat only when it is older than
RS_HEALTH_WORKER_MAX_AGE (180 s), which is more than any single vendor call
(60 s, or 300 s for an upload, which beats from busy()). A process with its own
worker thread judges only its own beats, so a new container can't fail on the
old one's last beat, and it reports "starting" until its first one: in the
first RS_HEALTH_WORKER_MAX_AGE seconds after a start, /health can fail only on a
dead worker thread or the database, so the deploy hook (which waits up to 120 s
after the switch) never rolls back a release for a slow vendor. Alert text carries ids only: no emails, names
or tokens.
"""
import contextlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("campaignify")

BACKUP_META = "backup_last_ok_at"       # ISO time of the last good off-site backup (M1.6 writes it)
HEARTBEAT_META = "worker_heartbeat"     # epoch seconds of the worker loop's last pass
ALERT_META = "alert:"                   # meta key prefix for dedupe state
SWEEP_SECONDS = 60
ASYNC = True                            # send in a thread so a slow mail API never holds a request; tests set False

# Webhook endpoints whose signature rejections are counted (by response, so the handlers stay untouched).
WEBHOOK_ENDPOINTS = {"stripe_webhook", "messaging.resend_webhook", "sms.inbound", "sms.status",
                     "cpools.partner_asset_url", "cpools.partner_event"}
REVERSAL_EVENTS = {"charge.refunded": "Refund", "charge.dispute.created": "Dispute opened",
                   "charge.dispute.closed": "Dispute closed"}


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def recipients() -> list[str]:
    raw = os.environ.get("OPS_ALERT_EMAIL") or ""
    return [a.strip() for a in raw.replace(";", ",").split(",") if "@" in a.strip()][:5]


def enabled() -> bool:
    return os.environ.get("OPS_ALERTS", "1") != "0"


# ---------------------------------------------------------------- sending, with dedupe

def _claim(key: str, window_s: float) -> tuple[bool, int]:
    """(send now?, repeats held back since the last send). Atomic across threads."""
    t = time.time()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM meta WHERE key=?", (ALERT_META + key,)).fetchone()
        try:
            st = json.loads(row["value"]) if row else {}
        except (ValueError, TypeError):
            st = {}
        last = float(st.get("last_at") or 0)
        if t - last < window_s:
            st["held"] = int(st.get("held") or 0) + 1
            send, held = False, st["held"]
        else:
            held = int(st.get("held") or 0)
            st = {"last_at": t, "held": 0, "sent": int(st.get("sent") or 0) + 1}
            send = True
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (ALERT_META + key, json.dumps(st)))
        conn.commit()
        return send, held
    finally:
        conn.close()


def fire(key: str, title: str, detail: str = "") -> bool:
    """Raise one alert. True when it went out now (False: deduped or alerts off)."""
    if not enabled():
        return False
    send, held = _claim(key, _num("OPS_ALERT_DEDUPE_MIN", 60) * 60)
    if not send:
        return False
    log.error("ALERT %s: %s %s", key, title, detail.replace("\n", " ")[:500])
    if held:
        detail = f"{detail}\n\nThis alert also fired {held} more time(s) since the last email."
    to = recipients()
    if not to:
        return True
    if ASYNC:
        threading.Thread(target=_deliver, args=(key, title, detail, to), name="rs-alert", daemon=True).start()
    else:
        _deliver(key, title, detail, to)
    return True


def _deliver(key: str, title: str, detail: str, to: list[str]):
    import mailer
    if not mailer.configured():
        log.warning("alert %s not emailed: email sending isn't connected", key)
        return
    from html import escape
    when = db.now()
    text = f"{title}\n\n{detail}\n\nAlert: {key}\nAt: {when} UTC\n"
    html = (f"<p><b>{escape(title)}</b></p><p>{escape(detail).replace(chr(10), '<br>')}</p>"
            f"<p style='color:#666'>Alert: {escape(key)}<br>At: {escape(when)} UTC</p>")
    bucket = int(time.time() // 60)
    for addr in to:
        try:
            mailer.send(frm=f'"Campaignify alerts" <alerts@{mailer.domain()}>', to=addr,
                        subject=f"[Campaignify] {title}"[:150], html=html, text=text,
                        idempotency_key=f"ops-{key}-{bucket}-{addr}", tags={"kind": "ops_alert"})
        except Exception as e:  # noqa: BLE001 — an alert must never break what raised it
            log.warning("alert %s not emailed: %s", key, str(e)[:200])


# ---------------------------------------------------------------- worker heartbeat

_beat = {"at": 0.0, "saved": 0.0}
STARTED = time.time()


def beat():
    """The worker loop calls this each pass (and between jobs). Kept in memory; written to meta every 10 s."""
    t = time.time()
    _beat["at"] = t
    if t - _beat["saved"] >= 10:
        _beat["saved"] = t
        try:
            with db.connect() as conn:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (HEARTBEAT_META, str(t)))
                conn.commit()
        except Exception:  # noqa: BLE001
            log.warning("couldn't record the worker heartbeat")


BUSY_TICK_S = 10
_busy = {"depth": 0, "since": 0.0, "what": None}
_ticker = [None]


def _in_worker() -> bool:
    import worker
    return worker._thread is not None and threading.current_thread() is worker._thread


@contextlib.contextmanager
def busy(what: str):
    """Around one long call in the worker loop: the heartbeat keeps going from a
    side thread while it runs, up to RS_HEALTH_BUSY_MAX_S (a call stuck longer
    than that stops the beats, so a hung loop still shows as late). Does nothing
    outside the worker thread (a web request's call says nothing about the worker)."""
    if not _in_worker():
        yield
        return
    if _busy["depth"] == 0:
        _busy.update(since=time.time(), what=what)
    _busy["depth"] += 1
    _start_ticker()
    try:
        yield
    finally:
        _busy["depth"] -= 1
        if _busy["depth"] <= 0:
            _busy.update(depth=0, since=0.0, what=None)
        beat()


def busy_tick() -> bool:
    """One tick of the side thread: beat if a call is in progress and not past the cap."""
    since = _busy["since"]
    if since and time.time() - since <= _num("RS_HEALTH_BUSY_MAX_S", 900):
        beat()
        return True
    return False


def _start_ticker():
    if _ticker[0] is not None and _ticker[0].is_alive():
        return

    def run():
        while True:
            time.sleep(BUSY_TICK_S)
            try:
                busy_tick()
            except Exception:  # noqa: BLE001
                log.warning("worker heartbeat tick failed")

    _ticker[0] = threading.Thread(target=run, name="rs-worker-beat", daemon=True)
    _ticker[0].start()


def worker_state(conn) -> dict:
    """{"ok", "status", "age_s"} for the worker loop (plus "busy" and "busy_s" while
    a long call runs). A process that doesn't run the worker (RS_WORKER=0) reads the
    heartbeat the worker's process saved, and with none reports "off" and passes.
    A process with its own worker thread goes by its own beats only."""
    import worker
    max_age = _num("RS_HEALTH_WORKER_MAX_AGE", 180)
    th = worker._thread
    if th is not None:
        last = _beat["at"]
        if not th.is_alive():
            return {"ok": False, "status": "stopped", "age_s": round(time.time() - last) if last else None}
        if not last:
            starting = time.time() - STARTED < max(60.0, max_age)
            return {"ok": starting, "status": "starting" if starting else "no heartbeat", "age_s": None}
    else:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (HEARTBEAT_META,)).fetchone()
            saved = float(row["value"]) if row and row["value"] else 0.0
        except (ValueError, TypeError):
            saved = 0.0
        last = max(_beat["at"], saved)
        if not last:
            if os.environ.get("RS_WORKER", "1") != "1":
                return {"ok": True, "status": "off", "age_s": None}
            starting = time.time() - STARTED < 60
            return {"ok": starting, "status": "starting" if starting else "no heartbeat", "age_s": None}
    age = time.time() - last
    out = {"ok": age <= max_age, "status": "ok" if age <= max_age else "late", "age_s": round(age)}
    if _busy["since"]:
        out.update(busy=_busy["what"], busy_s=round(time.time() - _busy["since"]))
    return out


# ---------------------------------------------------------------- /health

def disk_free_gb(path: str | None = None) -> float:
    try:
        return shutil.disk_usage(path or db.DATA).free / 1e9
    except OSError:
        return -1.0


def vendor_status() -> str:
    """The generation vendor's last probe (refreshed by sweep() every 5 minutes; /health never waits on it)."""
    import hf
    if not hf.configured():
        return "unconfigured"
    res, at = hf._probe.get("result"), hf._probe.get("at") or 0
    return res if res and time.time() - at < 900 else "unknown"


def health() -> tuple[dict, int]:
    """The body and status for /health: 503 when the database can't be read and
    written or the worker loop is late. Disk and vendor are reported (and alert)
    but don't fail it: a vendor outage shouldn't mark our container unhealthy."""
    checks, ok = {}, True
    worker_info = {"ok": True, "status": "unknown"}
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1 FROM workspaces LIMIT 1").fetchone()
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('health_probe', ?)", (db.now(),))
            conn.commit()
            checks["db"] = "ok"
            worker_info = worker_state(conn)
    except Exception as e:  # noqa: BLE001
        log.error("health: database check failed: %s", str(e)[:200])
        checks["db"] = "error"
        ok = False
    checks["worker"] = {k: v for k, v in worker_info.items() if k != "ok"}
    if not worker_info["ok"]:
        ok = False
        age = worker_info.get("age_s")
        if worker_info["status"] in ("stopped", "no heartbeat") or (age or 0) >= _num("OPS_WORKER_DOWN_MIN", 5) * 60:
            try:
                fire("worker_down", "The background worker isn't running",
                     f"Worker status: {worker_info['status']}; last pass {age if age is not None else 'unknown'} s ago. "
                     "Jobs aren't being submitted or finished. Restart the app container.")
            except Exception:  # noqa: BLE001
                log.exception("couldn't raise the worker alert")
    free = disk_free_gb()
    checks["disk"] = "ok" if free < 0 or free >= _num("OPS_ALERT_DISK_GB", 10) else "low"
    checks["vendor"] = vendor_status()
    return {"ok": ok, "checks": checks}, (200 if ok else 503)


# ---------------------------------------------------------------- the minute sweep (worker loop)

_last_sweep = [0.0]


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def maybe_sweep():
    """Run sweep() at most once a minute; never raises."""
    if not enabled() or time.time() - _last_sweep[0] < SWEEP_SECONDS:
        return
    _last_sweep[0] = time.time()
    try:
        sweep()
    except Exception:  # noqa: BLE001
        log.exception("alert sweep failed")


def sweep(probe: bool = True) -> list[str]:
    """Check every condition the worker can see. Returns the keys that fired."""
    fired = []
    with db.connect() as conn:
        for key, title, detail in _conditions(conn, probe):
            if fire(key, title, detail):
                fired.append(key)
    return fired


def _conditions(conn, probe: bool):
    stuck_min = _num("OPS_JOB_STUCK_MIN", 20)
    rows = conn.execute("SELECT id, status, tool FROM jobs WHERE status IN ('pending','running') AND created_at < ? "
                        "ORDER BY id LIMIT 20", (_ago(stuck_min),)).fetchall()
    if rows:
        ids = ", ".join(f"#{r['id']} {r['tool']} ({r['status']})" for r in rows[:10])
        yield ("jobs_stuck", f"{len(rows)} job(s) stuck over {int(stuck_min)} minutes",
               f"Jobs still pending or running after {int(stuck_min)} minutes: {ids}. "
               "They time out and refund at 120 minutes; check the vendor and the worker.")

    ended = conn.execute("SELECT COALESCE(SUM(status='failed'),0) f, COUNT(*) n FROM jobs "
                         "WHERE status IN ('done','failed') AND COALESCE(finished_at, updated_at) >= ? "
                         "AND COALESCE(error, '') NOT LIKE 'You canceled it.%'",   # a member's cancel isn't a vendor error
                         (_ago(15),)).fetchone()
    n, f = int(ended["n"] or 0), int(ended["f"] or 0)
    if n >= _num("OPS_VENDOR_MIN_JOBS", 5) and f * 100 > n * _num("OPS_VENDOR_ERROR_PCT", 20):
        yield ("vendor_errors", f"{f} of {n} jobs failed in 15 minutes",
               f"{round(100 * f / n)}% of the jobs that ended in the last 15 minutes failed. "
               "Credits were refunded. See the admin system card for the last error.")

    if probe:
        import hf
        if hf.configured():
            res = hf.probe(max_age=300)
            if res in ("rejected", "out_of_credit"):
                yield (f"vendor_{res}", "The generation vendor " + ("rejected our key" if res == "rejected"
                                                                     else "says we're out of credit"),
                       "New jobs will fail until this is fixed. Check the vendor account.")

    free = disk_free_gb()
    if 0 <= free < _num("OPS_ALERT_DISK_GB", 10):
        yield ("disk_low", f"Disk low: {free:.1f} GB free",
               f"{free:.1f} GB free where the database and media live (alert under "
               f"{_num('OPS_ALERT_DISK_GB', 10):g} GB). The disk is shared with Crowdcut.")

    row = conn.execute("SELECT value FROM meta WHERE key=?", (BACKUP_META,)).fetchone()
    if row and row["value"]:
        hours = _num("OPS_BACKUP_MAX_HOURS", 26)
        try:
            last = datetime.fromisoformat(row["value"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            late = datetime.now(timezone.utc) - last > timedelta(hours=hours)
        except ValueError:
            late = True
        if late:
            yield ("backup_late", "Backup is late",
                   f"The last good backup was at {row['value']} (alert after {hours:g} hours).")


def backup_ok(conn):
    """For the backup job: record a good backup (resets backup_late)."""
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (BACKUP_META, db.now()))
    conn.commit()


# ---------------------------------------------------------------- webhooks (watched by response)

def payment_reversed(kind: str, obj_id: str, workspace_id=None, amount_cents=None, currency: str = "usd",
                     note: str = "") -> bool:
    """A refund or dispute on one of our payments. The billing code may call this
    directly; the response watcher below also calls it, and the dedupe key keeps it to one email."""
    amt = f"{amount_cents / 100:.2f} {currency.upper()}" if isinstance(amount_cents, int) else "unknown amount"
    ws = f"workspace {workspace_id}" if workspace_id else "workspace not matched"
    return fire(f"payment_reversed:{obj_id}", f"{kind}: {amt}, {ws}",
                f"Stripe {kind.lower()} {obj_id} for {amt} ({ws}). {note}".strip())


def _our_payment(conn, obj: dict):
    """(is it ours?, workspace id) for a charge or dispute on the shared Stripe account."""
    meta = obj.get("metadata") or {}
    import billing
    if meta.get("app") == billing.APP_TAG:
        ws = meta.get("workspace_id")
        return True, int(ws) if str(ws or "").isdigit() else None
    pi = obj.get("payment_intent")
    pi = pi.get("id") if isinstance(pi, dict) else pi
    if pi:
        row = conn.execute("SELECT workspace_id FROM purchases WHERE stripe_payment_intent=? LIMIT 1", (pi,)).fetchone()
        if row:
            return True, row["workspace_id"]
        # a plan invoice's payment (stored at invoice.paid, N1)
        row = conn.execute("SELECT workspace_id FROM plan_invoices WHERE payment_intent=? LIMIT 1", (pi,)).fetchone()
        if row:
            return True, row["workspace_id"]
    if obj.get("id"):
        # billing_holds matched it, or parked it as one of our customers' (N1)
        row = conn.execute("SELECT workspace_id, ours FROM credit_reversals WHERE stripe_id=? LIMIT 1",
                           (obj["id"],)).fetchone()
        if row and (row["workspace_id"] or row["ours"]):
            return True, row["workspace_id"]
    cus = obj.get("customer")
    cus = cus.get("id") if isinstance(cus, dict) else cus
    if cus:
        row = conn.execute("SELECT id FROM workspaces WHERE stripe_customer_id=? LIMIT 1", (cus,)).fetchone()
        if row:
            return True, row["id"]
    inv = obj.get("invoice")
    inv = inv.get("id") if isinstance(inv, dict) else inv
    if inv:
        row = conn.execute("SELECT workspace_id FROM plan_invoices WHERE invoice_id=? LIMIT 1", (inv,)).fetchone()
        if row:
            return True, row["workspace_id"]
    return False, None


def watch_response(request, resp):
    """after_request: count webhook signature rejections and notice refunds and
    disputes, from the request and response only (the handlers stay as they are)."""
    ep = request.endpoint
    if ep not in WEBHOOK_ENDPOINTS or request.method != "POST" or not enabled():
        return
    try:
        if resp.status_code in (400, 401, 403) and not resp.direct_passthrough \
                and b"signature" in (resp.get_data() or b"")[:200].lower():
            db.hit("ops:webhook_sig")
            limit = int(_num("OPS_WEBHOOK_SIG_PER_HOUR", 10))
            if db.throttled("ops:webhook_sig", limit + 1, 3600):
                fire("webhook_signatures", f"Over {limit} webhook signature failures in an hour",
                     f"Last one on {request.path}. Either a signing secret is wrong after a change, "
                     "or someone is sending forged webhooks.")
        elif ep == "stripe_webhook" and 200 <= resp.status_code < 300:
            raw = request.get_data(cache=True) or b""
            if b'"charge.' not in raw:
                return
            ev = json.loads(raw)
            kind = REVERSAL_EVENTS.get(ev.get("type"))
            obj = (ev.get("data") or {}).get("object") or {}
            if not kind or not obj.get("id"):
                return
            out = resp.get_json(silent=True) or {}
            if out.get("parked") and out.get("ok"):
                return      # ours but not matched yet: billing_holds sent its own "not applied yet" alert (N1)
            with db.connect() as conn:
                ours, ws = _our_payment(conn, obj)
            if not ours:
                return
            amount = obj.get("amount_refunded") if ev["type"] == "charge.refunded" else obj.get("amount")
            note = f"Status: {obj.get('status')}." if ev["type"].startswith("charge.dispute") else ""
            if obj.get("reason"):
                note += f" Reason: {obj['reason']}."
            payment_reversed(kind, f"{ev['type']}:{obj['id']}", ws, amount if isinstance(amount, int) else None,
                             obj.get("currency") or "usd", note)
    except Exception:  # noqa: BLE001 — watching must never change the response
        log.exception("webhook watch failed")


def install(app):
    from flask import request

    @app.after_request
    def _ops_watch(resp):
        watch_response(request, resp)
        return resp

"""Email sending through Resend (plain HTTPS, no SDK), and its webhooks.

Shared sending domain (owner decision D6): mail from a workspace goes out as
"{Brand} via Campaignify" <ws-{id}@RS_MAIL_DOMAIN> with Reply-To set to the
customer. A workspace on Growth+ can verify its own domain (Phase 8,
sender_domains); then the From is its own.

Nothing here runs until RESEND_API_KEY and RS_MAIL_DOMAIN are set; webhooks
need RESEND_WEBHOOK_SECRET (svix signing). Tests replace `requests.post`.

Priority (Nov 8 2026: one Resend Free account, 100 a day and 10 requests a
second, shared with Crowdcut; once the quota was spent nobody got a code)

    transactional   tags kind: verify_email (the code), verify_signup (the
                    confirm-and-log-in link), password_reset, account_exists,
                    login_fails, ops_alert, and any other kind not nurture.
    nurture         NURTURE / NURTURE_PREFIXES: the first-run nudges ("Your 2
                    free ads are waiting", "Your free video ad is waiting") and
                    one-to-one outreach.
    workspace       no kind tag: a workspace's own flow emails (messaging.py),
                    logged in `messages`; paced and retried on a rate limit, and
                    a quota or rate refusal leaves them queued there (MailDeferred).

    * Pace: at most EMAIL_MAX_PER_SEC (default 3) calls a second. Account
      emails share one slot in `meta` (every thread and process); workspace
      sends pace per process. Crowdcut paces its own 4, under Resend's 10.
    * A 429 rate limit is retried in the call (EMAIL_RATE_RETRIES, default 2),
      waiting retry-after / ratelimit-reset when sent.
    * A daily or monthly quota error raises MailDeferred and logs `deferred`
      in email_log (retry_after: the header's time, else the next UTC midnight,
      or the 1st for the monthly quota) and sets a quota hold in `meta`:
      nurture stops until then; transactional mail still tries, and an
      accepted send clears the hold.
    * retry_deferred() (the nudge loop, every minute) resends each deferred
      transactional email once, oldest first, while still useful (RETRY_MINUTES;
      a newer email of the same kind to the address replaces it). Deferred
      nurture is dropped.
    * Nurture budget (nurture_block): none while a hold is on or a
      transactional email waits, past EMAIL_NURTURE_DAILY_MAX (default 60)
      nurture sends today (UTC), or once today's sends (account emails plus
      workspace emails) reach EMAIL_DAILY_LIMIT (default 100) minus
      EMAIL_TRANSACTIONAL_RESERVE (default 30). EMAIL_DAILY_LIMIT=0 or
      EMAIL_NURTURE_DAILY_MAX=0 turns that limit off.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

API = os.environ.get("RESEND_API_BASE", "https://api.resend.com").rstrip("/")
TIMEOUT = 20


log = logging.getLogger("campaignify")


class MailError(RuntimeError):
    pass


class MailDeferred(MailError):
    """The provider refused it for quota or rate, not for the message. queued: this
    module kept the copy and retry_deferred() sends it later; until: when the
    quota is expected back (None for a rate limit)."""

    def __init__(self, msg: str, until: datetime | None = None, queued: bool = False, code: str = ""):
        super().__init__(msg)
        self.until, self.queued, self.code = until, queued, code


COULD_NOT_SEND_CODE = "We couldn't send the code right now. Try again in a minute."


def api_key() -> str:
    return (os.environ.get("RESEND_API_KEY") or "").strip()


def domain() -> str:
    return (os.environ.get("RS_MAIL_DOMAIN") or "").strip().lower()


def configured() -> bool:
    return bool(api_key() and domain())


def webhook_secret() -> str:
    return (os.environ.get("RESEND_WEBHOOK_SECRET") or "").strip()


def _headers(idempotency_key: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json", "User-Agent": "campaignify/1.0"}
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key[:256]
    return h


def from_address(ws_id: int, sender_name: str, custom: str | None = None) -> str:
    """The From header: the workspace's own verified address, else the shared domain."""
    name = " ".join((sender_name or "").replace('"', "").split())[:60] or "Campaignify"
    if custom:
        return f'"{name}" <{custom}>'
    return f'"{name} via Campaignify" <ws-{ws_id}@{domain()}>'


# ---------------------------------------------------------------- priority

NURTURE = frozenset({"outreach", "weekly_report"})     # weekly_report: the Ad campaigns weekly email (M4.4)
NURTURE_PREFIXES = ("first_run_nudge_",)
# How long a deferred transactional email is still worth sending (minutes since it was first tried).
RETRY_MINUTES = {"verify_email": 15, "verify_signup": 48 * 60, "password_reset": 60, "account_exists": 60,
                 "login_fails": 60, "ops_alert": 12 * 60, "trial_reminder": 12 * 60}
DEFAULT_RETRY_MINUTES = 24 * 60
# A newer email of these kinds (a new code, nonce or token) replaces a deferred older one.
SUPERSEDED_BY_NEWER = frozenset({"verify_email", "verify_signup", "password_reset", "account_exists", "login_fails"})
RATE_DEFER_S = 60
MAX_WAIT_S = 5.0
HOLD_KEY, PACE_KEY = "mail_quota_hold", "mail_pace"

_clock = time.time          # tests swap these two for a fake clock
_sleep = time.sleep
_local = threading.Lock()
_local_next = [0.0]


def is_nurture(kind: str) -> bool:
    k = (kind or "").strip()
    return k in NURTURE or k.startswith(NURTURE_PREFIXES)


def kind_class(kind: str) -> str:
    if not (kind or "").strip():
        return "workspace"
    return "nurture" if is_nurture(kind) else "transactional"


def _num_env(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return float(default)


def daily_limit() -> int:
    """The provider's daily limit (EMAIL_DAILY_LIMIT, default 100 for Resend Free); 0 = none."""
    return max(0, int(_num_env("EMAIL_DAILY_LIMIT", 100)))


def transactional_reserve() -> int:
    return max(0, int(_num_env("EMAIL_TRANSACTIONAL_RESERVE", 30)))


def nurture_daily_max() -> int:
    """EMAIL_NURTURE_DAILY_MAX (default 60); 0 = no cap of its own."""
    return max(0, int(_num_env("EMAIL_NURTURE_DAILY_MAX", 60)))


def max_per_sec() -> float:
    return max(0.0, _num_env("EMAIL_MAX_PER_SEC", 3))


def rate_retries() -> int:
    return max(0, int(_num_env("EMAIL_RATE_RETRIES", 2)))


def _utcnow() -> datetime:
    return datetime.fromtimestamp(_clock(), timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(s):
    try:
        d = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _day_start(now: datetime) -> str:
    return _iso(now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))


def _conn(timeout: float = 3):
    """A short-lived connection for the log and the shared state. A short busy
    timeout: a caller holding a write must never stall a send for long."""
    import sqlite3

    import db
    c = sqlite3.connect(db.DB_PATH, timeout=timeout)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def _db(timeout: float = 3):
    c = _conn(timeout)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _nurture_sql() -> str:
    names = ",".join(f"'{k}'" for k in sorted(NURTURE))
    likes = " OR ".join(f"kind LIKE '{p}%'" for p in NURTURE_PREFIXES)
    return f"(kind IN ({names}) OR {likes})"


def _relevant(kind, created_at, now) -> bool:
    t = _parse(created_at)
    return bool(t) and now - t <= timedelta(minutes=RETRY_MINUTES.get(kind or "", DEFAULT_RETRY_MINUTES))


def hold_until(conn=None, now: datetime | None = None):
    """When the quota hold ends, or None."""
    now = now or _utcnow()
    own = conn is None
    try:
        conn = conn or _conn()
        r = conn.execute("SELECT value FROM meta WHERE key=?", (HOLD_KEY,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    finally:
        if own and conn is not None:
            conn.close()
    until = _parse(r["value"]) if r and r["value"] else None
    return until if until and until > now else None


def _set_hold(until: datetime, why: str):
    try:
        with _db() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (HOLD_KEY, _iso(until)))
        log.warning("email quota reached (%s): nurture paused until %s", why, _iso(until))
    except Exception:  # noqa: BLE001
        pass


def _clear_hold():
    try:
        with _db() as c:
            c.execute("UPDATE meta SET value=NULL WHERE key=? AND value IS NOT NULL", (HOLD_KEY,))
    except Exception:  # noqa: BLE001
        pass


def _waiting_count(conn, now) -> int:
    try:
        rows = conn.execute("SELECT kind, created_at FROM email_log WHERE status='deferred' AND payload IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for r in rows if _relevant(r["kind"], r["created_at"], now))


def counts_today(conn, now: datetime | None = None) -> dict:
    """Today's (UTC) accepted sends: account emails (email_log) plus workspace emails (messages)."""
    now = now or _utcnow()
    day = _day_start(now)
    out = {"sent": 0, "nurture": 0, "workspace": 0}
    try:
        r = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM({_nurture_sql()}), 0) nurture FROM email_log "
                         "WHERE status='sent' AND created_at >= ?", (day,)).fetchone()
        out.update(sent=r["n"] or 0, nurture=r["nurture"] or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["workspace"] = conn.execute("SELECT COUNT(*) FROM messages WHERE channel='email' AND status='sent' "
                                        "AND sent_at >= ?", (day,)).fetchone()[0] or 0
    except Exception:  # noqa: BLE001
        pass
    out["sent"] += out["workspace"]
    return out


def nurture_block(conn=None, now: datetime | None = None) -> str | None:
    """Why no nurture email may go out now, or None."""
    now = now or _utcnow()
    own = conn is None
    try:
        conn = conn or _conn()
    except Exception:  # noqa: BLE001
        return None
    try:
        until = hold_until(conn, now)
        if until:
            return f"email quota reached, waits until {until.strftime('%H:%M')} UTC"
        if _waiting_count(conn, now):
            return "transactional emails are waiting to be resent"
        c = counts_today(conn, now)
        cap = nurture_daily_max()
        if cap and c["nurture"] >= cap:
            return f"nurture cap reached ({c['nurture']} of {cap} today)"
        lim = daily_limit()
        if lim and c["sent"] >= max(0, lim - transactional_reserve()):
            return f"kept for transactional mail ({c['sent']} of {lim} sent today, {transactional_reserve()} reserved)"
        return None
    finally:
        if own:
            conn.close()


def status(conn) -> dict:
    """The /admin System card's email row."""
    now = _utcnow()
    out = {"configured": configured()}
    try:
        since = _iso(now - timedelta(days=1))
        r = conn.execute("SELECT COUNT(*) n, SUM(status='sent') sent, SUM(status='failed') failed, "
                         "SUM(status IN ('deferred','retrying','retried','dropped')) deferred, SUM(status='retried') resent, "
                         "SUM(status='deferred' AND payload IS NOT NULL) waiting, SUM(status='skipped') skipped "
                         "FROM email_log WHERE created_at > ?", (since,)).fetchone()
        out.update({k: r[k] or 0 for k in ("n", "sent", "failed", "deferred", "resent", "waiting", "skipped")})
        c = counts_today(conn, now)
        until = hold_until(conn, now)
        out.update(sent_today=c["sent"], nurture_today=c["nurture"], workspace_today=c["workspace"],
                   daily_limit=daily_limit(), reserve=transactional_reserve(), nurture_max=nurture_daily_max(),
                   hold_until=_iso(until) if until else None, nurture_paused=nurture_block(conn, now))
        last = conn.execute("SELECT subject, kind, status, error, created_at FROM email_log ORDER BY id DESC LIMIT 1").fetchone()
        out["last"] = dict(last) if last else None
    except Exception:  # noqa: BLE001 — the card must render even without the table
        pass
    return out


# ---------------------------------------------------------------- pace

def _slot_local(gap: float) -> float:
    with _local:
        t = _clock()
        slot = max(t, _local_next[0])
        _local_next[0] = slot + gap
        return slot


def _pace(shared: bool) -> float:
    r = max_per_sec()
    if r <= 0:
        return 0.0
    gap = 1.0 / r
    slot = None
    if shared:
        t = _clock()
        try:
            with _db(timeout=1) as c:
                c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                          "value=CAST(MAX(COALESCE(CAST(meta.value AS REAL), 0), ?) + ? AS TEXT)",
                          (PACE_KEY, str(t + gap), t, gap))
                slot = float(c.execute("SELECT value FROM meta WHERE key=?", (PACE_KEY,)).fetchone()["value"]) - gap
        except Exception:  # noqa: BLE001 — no table, or the file is busy: this process's own slot
            slot = None
    if slot is None:
        slot = _slot_local(gap)
    wait = slot - _clock()
    if wait > 0:
        _sleep(wait)
        return wait
    return 0.0


# ---------------------------------------------------------------- provider errors

def _header_wait(headers) -> float | None:
    if not headers:
        return None
    try:
        get = headers.get
    except AttributeError:
        return None
    for name in ("retry-after", "Retry-After", "ratelimit-reset", "RateLimit-Reset"):
        v = get(name)
        if v in (None, ""):
            continue
        v = str(v).strip()
        try:
            n = float(v)
        except ValueError:
            try:
                n = (parsedate_to_datetime(v) - _utcnow()).total_seconds()
            except (TypeError, ValueError):
                continue
        if n > 1e9:
            n -= _clock()
        return max(0.0, n)
    return None


def _quota_until(code: str, headers, now: datetime) -> datetime:
    w = _header_wait(headers)
    if w and w > 60:
        return now + timedelta(seconds=w)
    if code == "quota_monthly":
        first = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (first + timedelta(days=32)).replace(day=1)
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _attempt(body: dict, idempotency_key: str | None, shared: bool):
    """Pace, post, retry a rate limit in the call. Returns (outcome, error, until, id):
    sent | rate_limit | quota_daily | quota_monthly | failed."""
    tries = rate_retries() + 1
    for i in range(tries):
        _pace(shared)
        try:
            r = requests.post(f"{API}/emails", json=body, headers=_headers(idempotency_key), timeout=TIMEOUT)
        except requests.RequestException as e:
            return "failed", f"Couldn't reach the email service: {e}", None, ""
        if r.status_code < 400:
            try:
                return "sent", "", None, (r.json() or {}).get("id") or ""
            except Exception:  # noqa: BLE001
                return "sent", "", None, ""
        text = (getattr(r, "text", "") or "")[:300]
        name = ""
        try:
            name = str((r.json() or {}).get("name") or "")
        except Exception:  # noqa: BLE001
            pass
        low = (name + " " + text).lower()
        hdrs = getattr(r, "headers", None) or {}
        err = f"{r.status_code}: {text[:200]}"
        if "monthly_quota" in low or ("quota" in low and "month" in low):
            return "quota_monthly", err, _quota_until("quota_monthly", hdrs, _utcnow()), ""
        if "quota" in low:
            return "quota_daily", err, _quota_until("quota_daily", hdrs, _utcnow()), ""
        if r.status_code != 429:
            return "failed", err, None, ""
        if i == tries - 1:
            return "rate_limit", err, None, ""
        w = _header_wait(hdrs)
        _sleep(min(MAX_WAIT_S, w if w is not None else 0.5 * (2 ** i)))
    return "failed", "no attempt", None, ""


def _log(to, subject, kind, status_, error="", retry_after=None, payload=None):
    try:
        import db
        with _db() as c:
            c.execute("INSERT INTO email_log (to_addr, subject, kind, status, error, created_at, retry_after, payload) "
                      "VALUES (?,?,?,?,?,?,?,?)", (to, (subject or "")[:200], kind, status_, (error or "")[:300], db.now(),
                                                   _iso(retry_after) if retry_after else None, payload))
    except Exception:  # noqa: BLE001 — logging never stops a send
        pass


def send(*, frm: str, to: str, subject: str, html: str, text: str, reply_to: str | None = None,
         headers: dict | None = None, idempotency_key: str | None = None, tags: dict | None = None) -> str:
    """One email. Returns the provider's message id; raises MailError (MailDeferred
    for a quota or rate refusal). tags["kind"] names an account email (see the
    module notes); without it, it is a workspace email."""
    if not configured():
        raise MailError("Email sending isn't connected.")
    kind = str((tags or {}).get("kind") or "")
    cls = kind_class(kind)
    if cls == "nurture":
        why = nurture_block()
        if why:
            _log(to, subject, kind, "skipped", f"nurture paused: {why}")
            raise MailError(f"Not sent: {why}.")
    body = {"from": frm, "to": [to], "subject": subject, "html": html, "text": text, "headers": headers or {}}
    if reply_to:
        body["reply_to"] = reply_to
    if tags:
        body["tags"] = [{"name": k, "value": str(v)[:256]} for k, v in tags.items()]
    outcome, err, until, pid = _attempt(body, idempotency_key, shared=cls != "workspace")
    logged = cls != "workspace"
    if outcome == "sent":
        if logged:
            _log(to, subject, kind, "sent")
        _clear_hold()
        return pid
    keep = cls == "transactional"
    copy = json.dumps({"body": body, "key": idempotency_key}, ensure_ascii=False) if keep else None
    if outcome.startswith("quota"):
        _set_hold(until, outcome)
        if logged:
            _log(to, subject, kind, "deferred", err, retry_after=until, payload=copy)
        raise MailDeferred(f"The daily email limit is reached ({err})", until=until, queued=keep, code=outcome)
    if outcome == "rate_limit":
        if logged:
            _log(to, subject, kind, "deferred" if keep else "failed", err,
                 retry_after=_utcnow() + timedelta(seconds=RATE_DEFER_S) if keep else None, payload=copy)
        raise MailDeferred(f"The email service is busy ({err})", queued=keep, code=outcome)
    if logged:
        _log(to, subject, kind, "failed", err)
    raise MailError(err)


def retry_deferred(now: datetime | None = None, limit: int = 30) -> dict:
    """Resend deferred transactional emails once the provider takes mail again:
    oldest first, each once, only while useful. The nudge loop calls it every minute."""
    st = {"resent": 0, "dropped": 0, "failed": 0, "waiting": 0}
    if not configured():
        return st
    now = now or _utcnow()
    try:
        c = _conn()
    except Exception:  # noqa: BLE001
        return st
    try:
        try:
            rows = c.execute("SELECT * FROM email_log WHERE status='deferred' AND payload IS NOT NULL "
                             "ORDER BY id LIMIT 500").fetchall()
        except Exception:  # noqa: BLE001
            return st
        held = hold_until(c, now) is not None
        done = 0
        for r in rows:
            if not _relevant(r["kind"], r["created_at"], now) or is_nurture(r["kind"]):
                c.execute("UPDATE email_log SET status='dropped', payload=NULL WHERE id=? AND status='deferred'", (r["id"],))
                c.commit()
                st["dropped"] += 1
                continue
            if r["kind"] in SUPERSEDED_BY_NEWER and c.execute(
                    "SELECT 1 FROM email_log WHERE to_addr=? AND kind=? AND id>? AND status IN ('sent','retried','deferred') "
                    "LIMIT 1", (r["to_addr"], r["kind"], r["id"])).fetchone():
                c.execute("UPDATE email_log SET status='dropped', payload=NULL, error='a newer one replaced it' WHERE id=?",
                          (r["id"],))
                c.commit()
                st["dropped"] += 1
                continue
            ra = _parse(r["retry_after"])
            if held or done >= limit or (ra and ra > now and "quota" not in (r["error"] or "").lower()):
                st["waiting"] += 1
                continue
            cur = c.execute("UPDATE email_log SET status='retrying' WHERE id=? AND status='deferred'", (r["id"],))
            c.commit()
            if cur.rowcount != 1:
                continue
            done += 1
            try:
                saved = json.loads(r["payload"])
                body, key = saved["body"], saved.get("key")
            except (TypeError, ValueError, KeyError):
                c.execute("UPDATE email_log SET status='failed', payload=NULL, error='unreadable copy' WHERE id=?", (r["id"],))
                c.commit()
                st["failed"] += 1
                continue
            outcome, err, until, _pid = _attempt(body, (key + "-r") if key else None, shared=True)
            stamp = _iso(_utcnow())
            if outcome == "sent":
                c.execute("UPDATE email_log SET status='retried', payload=NULL, retried_at=?, error=? WHERE id=?",
                          (stamp, f"resent {stamp}", r["id"]))
                c.commit()
                _log(r["to_addr"], r["subject"], r["kind"], "sent", f"resent from #{r['id']}")
                _clear_hold()
                st["resent"] += 1
                continue
            if outcome.startswith("quota") or outcome == "rate_limit":
                if outcome.startswith("quota"):
                    _set_hold(until, outcome)
                c.execute("UPDATE email_log SET status='deferred', retry_after=?, error=? WHERE id=?",
                          (_iso(until or _utcnow() + timedelta(seconds=RATE_DEFER_S)), err[:300], r["id"]))
                c.commit()
                st["waiting"] += 1
                held = True
                continue
            c.execute("UPDATE email_log SET status='failed', payload=NULL, retried_at=?, error=? WHERE id=?",
                      (stamp, err[:300], r["id"]))
            c.commit()
            st["failed"] += 1
        return st
    finally:
        c.close()


def delivered(to: str, kind: str) -> bool | None:
    """Did the newest `kind` email to this address reach the provider? None when
    there is no log row (an older database, or nothing sent yet)."""
    try:
        with _db() as c:
            r = c.execute("SELECT status FROM email_log WHERE to_addr=? AND kind=? ORDER BY id DESC LIMIT 1",
                          (to, kind)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return None if r is None else r["status"] in ("sent", "retried")


# ---------------------------------------------------------------- domains (Phase 8)

def add_domain(name: str) -> dict:
    r = requests.post(f"{API}/domains", json={"name": name}, headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise MailError(f"{r.status_code}: {r.text[:200]}")
    return r.json() or {}


def verify_domain(domain_id: str) -> dict:
    r = requests.post(f"{API}/domains/{domain_id}/verify", json={}, headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise MailError(f"{r.status_code}: {r.text[:200]}")
    return get_domain(domain_id)


def get_domain(domain_id: str) -> dict:
    r = requests.get(f"{API}/domains/{domain_id}", headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise MailError(f"{r.status_code}: {r.text[:200]}")
    return r.json() or {}


# ---------------------------------------------------------------- webhooks (svix)

def verify_webhook(headers, raw: bytes, tolerance: int = 300, now: float | None = None) -> bool:
    """Resend signs webhooks the svix way: base64 HMAC-SHA256 of "id.timestamp.body"
    with the secret after its "whsec_" prefix; svix-signature lists "v1,<sig>" values."""
    secret = webhook_secret()
    msg_id, ts, sigs = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature") or ""
    if not (secret and msg_id and ts and sigs):
        return False
    try:
        if abs((now or time.time()) - int(ts)) > tolerance:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except (ValueError, TypeError):
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, s.split(",", 1)[1]) for s in sigs.split() if s.startswith("v1,"))


def sign_webhook(secret: str, msg_id: str, ts: str, raw: bytes) -> str:
    """For tests: the svix-signature header for a body."""
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    return "v1," + base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()

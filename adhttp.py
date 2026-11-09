"""Every HTTP call to an ad platform goes through here (roadmap M2.1, design doc section 3).

    call(platform, method, url, params=..., data=..., headers=..., account=..., cost=...)

What it does for each call:
  * a timeout (TIMEOUT seconds) on every request;
  * retries with jittered backoff on network errors, 5xx and the platforms' rate-limit and
    "temporary" error codes (Meta 1, 2, 4, 17, 32, 341, 613, 80000 to 80014; Google
    RESOURCE_EXHAUSTED, INTERNAL_ERROR, TRANSIENT_ERROR, CONCURRENT_MODIFICATION; TikTok 40100), at most
    RETRIES attempts;
  * Google Ads errors (a GoogleAdsFailure in error.details) come back with `code` set to
    "<category>.<ENUM>", e.g. "authenticationError.OAUTH_TOKEN_EXPIRED", and the request id as `trace`;
  * a per-(platform, ad account) point budget in the api_budget table, so we never pass the
    Meta Limited tier's 60 points (read 1, write 3, decaying over 300 s; research B1). A call
    that would pass it waits up to `max_wait` seconds, then raises RateLimited;
  * the usage headers Meta sends back (x-business-use-case-usage, x-ad-account-usage): when Meta
    says an account is throttled, the account is blocked here for as long as Meta says;
  * edit_lock(): Meta allows one edit per object per 30 s (subcode 4841018). call(edit_of=<object id>)
    takes the object's slot first; the launch saga (launches.py, M2.3) passes it on every change to an
    existing object (status, delete);
  * no secrets in logs or errors. A log line has the platform, method, path (never the query),
    status, error code and time. `json=` sends a JSON body (Google Ads REST). Error messages are scrubbed of access tokens, app secrets,
    authorization codes and appsecret_proof, and a network error is re-raised without the
    underlying exception (requests puts the full URL, query and all, in its messages).

Tests replace TRANSPORT (and SLEEP, CLOCK) with fakes: no request leaves the machine.
"""
import json
import logging
import os
import random
import re
import sqlite3
import time
from urllib.parse import urlsplit

import requests

import db

log = logging.getLogger("campaignify.adhttp")

TIMEOUT = 20
RETRIES = 3
BACKOFF = 1.0            # seconds before the 2nd attempt; doubled each time, plus up to 50% jitter
SLEEP = time.sleep       # tests make these no-ops / a fake clock
CLOCK = time.time

# Meta ad-account scoring (research B1): Limited tier 60 points, Full 9,000; points decay over 300 s.
# We stop at 90% of the cap. AD_RATE_TIER=full once App Review grants Full access.
DECAY_S = 300.0
CAPS = {"limited": 60.0, "full": 9000.0}
MARGIN = 0.9
EDIT_LOCK_S = 30.0

SECRET_PARAMS = ("access_token", "client_secret", "code", "code_verifier", "fb_exchange_token", "input_token",
                 "appsecret_proof", "refresh_token", "secret", "password")
_SECRET_RX = re.compile(r"(?i)\b(" + "|".join(SECRET_PARAMS) + r")=([^&\s\"']+)")
_BEARER_RX = re.compile(r"(?i)\b(bearer|oauth)\s+[A-Za-z0-9._\-|]+")

META_TRANSIENT = {1, 2, 4, 17, 32, 341, 613} | set(range(80000, 80015))
META_AUTH = {102, 190}
META_PERMISSION = {10, 200, 294}
# Google Ads: the per-customer budget here is a safety net under Google's own limits (Basic access is
# 15,000 operations a day per Cloud project; research B2), not Meta's ad-account score.
PLATFORM_CAPS = {"google": 600.0}
GOOGLE_TRANSIENT = {"internalError.INTERNAL_ERROR", "internalError.TRANSIENT_ERROR", "internalError.DEADLINE_EXCEEDED",
                    "databaseError.CONCURRENT_MODIFICATION", "quotaError.RESOURCE_TEMPORARILY_EXHAUSTED",
                    "quotaError.RESOURCE_EXHAUSTED"}
GOOGLE_AUTH = {"authenticationError.OAUTH_TOKEN_EXPIRED", "authenticationError.OAUTH_TOKEN_REVOKED",
               "authenticationError.OAUTH_TOKEN_INVALID", "authenticationError.OAUTH_TOKEN_DISABLED",
               "authenticationError.AUTHENTICATION_ERROR", "authenticationError.OAUTH_TOKEN_HEADER_INVALID"}
GOOGLE_PERMISSION = {"authorizationError.USER_PERMISSION_DENIED", "authorizationError.ACTION_NOT_PERMITTED",
                     "authorizationError.CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION",
                     "authorizationError.DEVELOPER_TOKEN_NOT_APPROVED", "authorizationError.CUSTOMER_NOT_ENABLED",
                     "authenticationError.NOT_ADS_USER"}


class AdHttpError(Exception):
    """A platform call that failed. str() is safe to log; `user_message` is what we may show."""

    def __init__(self, platform, message, status=None, code=None, subcode=None, trace=None, user_message=None):
        super().__init__(scrub(message))
        self.platform, self.status, self.code, self.subcode, self.trace = platform, status, code, subcode, trace
        self.user_message = scrub(user_message or "")


class TokenInvalid(AdHttpError):
    """The token expired or was revoked (Meta 190 / 102, HTTP 401): the connection needs Reconnect."""


class PermissionDenied(AdHttpError):
    """The token works but lacks a permission for this call (Meta 10 / 200)."""


class RateLimited(AdHttpError):
    """Our budget or the platform says wait. `retry_after` is in seconds."""

    def __init__(self, platform, message, retry_after=0.0, **kw):
        super().__init__(platform, message, **kw)
        self.retry_after = float(retry_after or 0)


class NetworkError(AdHttpError):
    """No usable answer (connection, timeout, not JSON) after every retry."""


def scrub(text, extra=()) -> str:
    """`text` with every token-like value replaced by ***."""
    text = str(text or "")
    for s in extra:
        if s and len(str(s)) >= 6:
            text = text.replace(str(s), "***")
    text = _SECRET_RX.sub(lambda m: m.group(1) + "=***", text)
    return _BEARER_RX.sub(lambda m: m.group(1) + " ***", text)


class Response:
    def __init__(self, status, headers, body, text=""):
        self.status, self.headers, self.body, self.text = status, headers or {}, body, text


def _requests_transport(method, url, params=None, data=None, headers=None, timeout=TIMEOUT, files=None):
    r = requests.request(method, url, params=params, data=data, headers=headers, timeout=timeout, files=files)
    try:
        body = r.json()
    except ValueError:
        body = None
    return Response(r.status_code, dict(r.headers), body, r.text[:2000] if body is None else "")


TRANSPORT = _requests_transport


def tier() -> str:
    t = (os.environ.get("AD_RATE_TIER") or "limited").strip().lower()
    return t if t in CAPS else "limited"


# ---------------------------------------------------------------- budget (api_budget table)

def _budget_row(conn, platform, account):
    return conn.execute("SELECT * FROM api_budget WHERE platform=? AND account=?", (platform, account)).fetchone()


def take(platform: str, account: str, cost: float, max_wait: float = 0.0):
    """Spend `cost` points of (platform, account)'s budget, waiting up to max_wait seconds for it
    to decay. Raises RateLimited when it can't. Safe across processes (BEGIN IMMEDIATE)."""
    full = PLATFORM_CAPS.get(platform) or CAPS[tier()]
    cap = full * MARGIN
    rate = full / DECAY_S
    deadline = CLOCK() + max(0.0, max_wait)
    while True:
        now = CLOCK()
        conn = sqlite3.connect(db.DB_PATH, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = _budget_row(conn, platform, account)
            points = max(0.0, (r["points"] if r else 0.0) - (now - (r["at"] if r else now)) * rate)
            blocked = (r["blocked_until"] if r else 0.0) or 0.0
            wait = max(blocked - now, (points + cost - cap) / rate if points + cost > cap else 0.0)
            if wait <= 0:
                conn.execute("INSERT INTO api_budget (platform, account, points, at, blocked_until) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(platform, account) DO UPDATE SET points=excluded.points, at=excluded.at",
                             (platform, account, points + cost, now, blocked))
                conn.execute("COMMIT")
                return
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        if now + wait > deadline:
            raise RateLimited(platform, f"{platform} budget for {account} is spent; wait {int(wait) + 1} s",
                              retry_after=wait, user_message=f"{_word(platform)} asked us to slow down. Try again in a "
                                                             "few minutes.")
        SLEEP(min(wait, max(0.0, deadline - now)) + 0.01)


def block(platform: str, account: str, seconds: float):
    """Nothing more for (platform, account) for `seconds` (the platform told us to stop)."""
    until = CLOCK() + max(0.0, seconds)
    with db.connect() as conn:
        conn.execute("INSERT INTO api_budget (platform, account, points, at, blocked_until) VALUES (?,?,0,?,?) "
                     "ON CONFLICT(platform, account) DO UPDATE SET blocked_until=MAX(blocked_until, excluded.blocked_until)",
                     (platform, account, CLOCK(), until))
        conn.commit()


def edit_lock(platform: str, object_id: str, max_wait: float = 0.0):
    """Take the 30 s edit slot for one object (Meta: one edit per object per 30 s). Waits up to
    max_wait seconds, else RateLimited."""
    key = f"edit:{object_id}"
    deadline = CLOCK() + max(0.0, max_wait)
    while True:
        now = CLOCK()
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            r = _budget_row(conn, platform, key)
            free_at = (r["blocked_until"] if r else 0.0) or 0.0
            if free_at <= now:
                conn.execute("INSERT INTO api_budget (platform, account, points, at, blocked_until) VALUES (?,?,0,?,?) "
                             "ON CONFLICT(platform, account) DO UPDATE SET at=excluded.at, blocked_until=excluded.blocked_until",
                             (platform, key, now, now + EDIT_LOCK_S))
                conn.commit()
                return
            conn.rollback()
        if free_at > deadline:
            raise RateLimited(platform, f"{object_id} was edited under {int(EDIT_LOCK_S)} s ago",
                              retry_after=free_at - now, user_message="Meta allows one change per item every 30 seconds.")
        SLEEP(free_at - now + 0.01)


def _read_usage(platform, account, headers):
    """Meta's usage headers: block the account here when Meta says it is throttled."""
    if platform != "meta" or not account:
        return
    h = {k.lower(): v for k, v in (headers or {}).items()}
    wait = 0.0
    try:
        buc = json.loads(h.get("x-business-use-case-usage") or "{}")
        for entries in (buc.values() if isinstance(buc, dict) else []):
            for e in entries or []:
                wait = max(wait, float(e.get("estimated_time_to_regain_access") or 0) * 60)
                if max(float(e.get("call_count") or 0), float(e.get("total_cputime") or 0),
                       float(e.get("total_time") or 0)) >= 95:
                    wait = max(wait, 60.0)
        acc = json.loads(h.get("x-ad-account-usage") or "{}")
        if isinstance(acc, dict) and float(acc.get("acc_id_util_pct") or 0) >= 95:
            wait = max(wait, float(acc.get("reset_time_duration") or 60))
    except (ValueError, TypeError, AttributeError):
        return
    if wait > 0:
        log.warning("adhttp %s %s throttled by the platform for %d s", platform, account, int(wait))
        block(platform, account, wait)


# ---------------------------------------------------------------- errors

def _word(platform) -> str:
    return {"meta": "Meta", "google": "Google"}.get(platform, "The ad platform")


def _google_failure(body: dict):
    """(code "category.ENUM" or None, Google's message, request id, retry seconds) from a Google Ads error body."""
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = msg = req = None
    retry = 0.0
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        req = req or d.get("requestId")
        for e in d.get("errors") or []:
            ec = e.get("errorCode") if isinstance(e, dict) else None
            if isinstance(ec, dict) and ec and code is None:
                cat, val = next(iter(ec.items()))
                code, msg = f"{cat}.{val}", e.get("message")
            q = ((e.get("details") or {}).get("quotaErrorDetails") or {}) if isinstance(e, dict) else {}
            m = re.match(r"^(\d+(?:\.\d+)?)s$", str(q.get("retryDelay") or ""))
            if m:
                retry = max(retry, float(m.group(1)))
    return code, (msg or err.get("message") or ""), req, retry


def _classify_google(resp, body, secrets):
    if isinstance(body.get("error"), str):        # the OAuth endpoints: {"error": "invalid_grant", ...}
        if resp.status < 400 and not body.get("error"):
            return None, False
        code = str(body["error"])[:60]
        msg = scrub(f"google {resp.status}: {code}: {str(body.get('error_description') or '')[:200]}", secrets)
        if code in ("invalid_grant", "invalid_token", "unauthorized_client"):
            return TokenInvalid("google", msg, status=resp.status, code=f"oauth.{code}"), False
        return AdHttpError("google", msg, status=resp.status, code=f"oauth.{code}"), resp.status >= 500
    if resp.status < 400:
        return None, False
    code, gmsg, req, retry = _google_failure(body)
    status = str((body.get("error") or {}).get("status") or "") if isinstance(body.get("error"), dict) else ""
    msg = scrub(f"google {resp.status} {status} {code or ''}: {gmsg or resp.text or 'no message'}"[:400], secrets)
    kw = dict(status=resp.status, code=code, trace=req, user_message=scrub(gmsg, secrets))
    if code in GOOGLE_AUTH or (resp.status == 401 and code != "authenticationError.NOT_ADS_USER"):
        return TokenInvalid("google", msg, **kw), False
    if code in GOOGLE_PERMISSION:
        return PermissionDenied("google", msg, **kw), False
    if resp.status == 429 or (code or "").startswith("quotaError.") or status == "RESOURCE_EXHAUSTED":
        return RateLimited("google", msg, retry_after=retry or 60, **kw), True
    if code in GOOGLE_TRANSIENT or resp.status >= 500:
        return AdHttpError("google", msg, **kw), True
    return AdHttpError("google", msg, **kw), False


def _classify(platform, resp, secrets):
    """(error to raise or None, retryable?) for a response."""
    body = resp.body if isinstance(resp.body, dict) else {}
    if platform == "meta":
        err = body.get("error") if isinstance(body.get("error"), dict) else None
        if err is None and resp.status < 400:
            return None, False
        err = err or {}
        code, sub = err.get("code"), err.get("error_subcode")
        msg = f"meta {resp.status} code={code} subcode={sub}: {err.get('message') or resp.text or 'no message'}"[:400]
        kw = dict(status=resp.status, code=code, subcode=sub, trace=err.get("fbtrace_id"),
                  user_message=scrub(err.get("error_user_msg") or err.get("message") or "", secrets))
        if code in META_AUTH or resp.status == 401:
            return TokenInvalid(platform, scrub(msg, secrets), **kw), False
        if code in META_PERMISSION:
            return PermissionDenied(platform, scrub(msg, secrets), **kw), False
        if sub == 4841018:
            return RateLimited(platform, scrub(msg, secrets), retry_after=EDIT_LOCK_S, **kw), True
        if code in META_TRANSIENT or err.get("is_transient") or resp.status >= 500:
            return RateLimited(platform, scrub(msg, secrets), retry_after=60, **kw) if code in (4, 17, 32, 613) \
                else AdHttpError(platform, scrub(msg, secrets), **kw), True
        return AdHttpError(platform, scrub(msg, secrets), **kw), False
    if platform == "google":
        return _classify_google(resp, body, secrets)
    # TikTok / others: HTTP status first, then their throttle markers.
    if resp.status < 400 and not (platform == "tiktok" and body.get("code") not in (None, 0)):
        return None, False
    text = json.dumps(body)[:400] if body else (resp.text or "")[:400]
    msg = scrub(f"{platform} {resp.status}: {text}", secrets)
    if resp.status == 401:
        return TokenInvalid(platform, msg, status=resp.status), False
    if resp.status == 429 or "RESOURCE_EXHAUSTED" in text or body.get("code") == 40100:
        return RateLimited(platform, msg, retry_after=60, status=resp.status), True
    return AdHttpError(platform, msg, status=resp.status), resp.status >= 500


def call(platform: str, method: str, url: str, *, params=None, data=None, headers=None, files=None,
         account: str = None, cost: float = None, max_wait: float = 0.0, retries: int = None, secrets=(),
         edit_of: str = None, json_body=None):
    """The JSON body of one platform call, or an AdHttpError. `account` names the budget to
    spend from (an ad account id); `secrets` are extra values to scrub from any message; `edit_of`
    is an existing object this call changes (it waits for, then takes, that object's 30 s edit slot).
    Calls that create something should pass retries=1: a timeout may hide a create that worked, so
    the caller looks the object up before trying again (launches.py does)."""
    method = method.upper()
    if json_body is not None:
        data = json.dumps(json_body, separators=(",", ":"))
        headers = dict(headers or {}, **{"Content-Type": "application/json"})
    if edit_of:
        edit_lock(platform, str(edit_of), max_wait=max(max_wait, EDIT_LOCK_S + 1))
    cost = cost if cost is not None else (1.0 if method == "GET" else 3.0)
    path = urlsplit(url).path
    attempts = max(1, retries or RETRIES)
    extra = [v for k, v in (params or {}).items() if k in SECRET_PARAMS] + \
            [v for k, v in (data.items() if isinstance(data, dict) else ()) if k in SECRET_PARAMS] + list(secrets)
    last = None
    for n in range(attempts):
        if account:
            try:
                take(platform, account, cost, max_wait=max_wait)
            except RateLimited:
                if isinstance(last, RateLimited):    # the platform said wait: keep its code and request id
                    raise last from None
                raise
        t0 = CLOCK()
        try:
            resp = TRANSPORT(method, url, params=params, data=data, headers=headers, timeout=TIMEOUT, files=files)
        except (requests.RequestException, OSError) as e:
            # requests' messages carry the whole URL (query included): keep only the kind of failure.
            last = NetworkError(platform, f"{platform} {method} {path}: {type(e).__name__}",
                                user_message=f"We couldn't reach {_word(platform).replace('The ad', 'the ad')}. "
                                             "Try again in a minute.")
            log.warning("adhttp %s %s %s network error %s (attempt %d)", platform, method, path, type(e).__name__, n + 1)
        else:
            _read_usage(platform, account, resp.headers)
            err, retry = _classify(platform, resp, extra)
            log.info("adhttp %s %s %s -> %s%s in %d ms", platform, method, path, resp.status,
                     f" code={err.code}" if err is not None and err.code is not None else "", int((CLOCK() - t0) * 1000))
            if err is None:
                if resp.body is None:
                    last = NetworkError(platform, f"{platform} {method} {path}: answer is not JSON")
                else:
                    return resp.body
            elif not retry:
                raise err
            else:
                last = err
        if n + 1 < attempts:
            wait = BACKOFF * (2 ** n) * (1 + random.random() * 0.5)
            if isinstance(last, RateLimited) and last.retry_after and account:
                block(platform, account, min(last.retry_after, 300))
            SLEEP(wait)
    raise last

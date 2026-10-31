"""Server-side copies of our own ad conversions: Meta Conversions API, TikTok Events API
and Reddit Conversions API (modeled on Crowdcut's conversions.py).

SignUp and Purchase go to each platform from the server as well as from its pixel
(pixels.py), with the same event id on both sides (signup-<user id>, purchase-<Checkout
Session id>), so each platform counts the conversion once and a blocked pixel or a closed
tab doesn't lose it. Purchase is sent from the Stripe webhook (checkout.session.completed),
so it arrives even when the buyer never comes back to the billing page.

Optional. A platform is skipped until both its pixel id and its token are set:

    META_CAPI_TOKEN       Meta Events Manager > the dataset > Settings > Conversions API > Generate access token
    TIKTOK_EVENTS_TOKEN   TikTok Events Manager > the pixel > Settings > Events API > Generate access token
    REDDIT_CAPI_TOKEN     Reddit Ads > Events Manager > Conversions API > Generate access token
    META_TEST_EVENT_CODE / TIKTOK_TEST_EVENT_CODE / REDDIT_CAPI_TEST=1
                          send to the platforms' test tools instead (for checking deduplication)

X has no server-side copy (its Conversions API needs OAuth app credentials); the X pixel covers it.

What is sent: the event name, time, event id, the page address without its query, the visitor's
IP address and browser user agent, the platform's own first-party cookie and ad click id when
present, and for Purchase the amount and currency. Never the email, name or account content.
Visitors the pixels skip for consent (pixels.opted_out: GPC, Do Not Track, EEA/UK/CH, a time
zone in Europe) are skipped here too. Rows are queued once per platform and event id
(conversion_queue), sent on a daemon thread and retried by the worker. Nothing here ever raises
into a request or the Stripe webhook.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import db
import gads
import pixels

log = logging.getLogger("campaignify")

TOKEN_ENV = {"meta": "META_CAPI_TOKEN", "tiktok": "TIKTOK_EVENTS_TOKEN", "reddit": "REDDIT_CAPI_TOKEN"}
EVENTS = ("SignUp", "Purchase")
MAX_ATTEMPTS = 4
SITE = "https://campaignify.ai/"


def _token(platform: str) -> str:
    return (os.environ.get(TOKEN_ENV[platform]) or "").strip()


def enabled_platforms() -> list[str]:
    p = pixels.ids()
    return [k for k in ("meta", "tiktok", "reddit") if _token(k) and p[k]]


def context(req) -> dict | None:
    """The visitor's browser context for a server-side event, or None when it must not be sent
    (consent) or nothing would send it (no token set)."""
    if not enabled_platforms() or pixels.opted_out(req):
        return None
    ck = req.cookies
    ft = gads.parse_cookie(ck.get(gads.COOKIE))
    fbc = ck.get("_fbc") or (f"fb.1.{int(time.time() * 1000)}.{ft['fbclid']}" if ft.get("fbclid") else None)
    return {k: v for k, v in {
        "ip": (req.remote_addr or "")[:64], "ua": (req.user_agent.string or "")[:400],
        "url": (req.host_url.rstrip("/") + req.path)[:300],   # never the query string
        "fbp": ck.get("_fbp"), "fbc": fbc, "ttp": ck.get("_ttp"), "ttclid": ft.get("ttclid"),
        "rdt_uuid": ck.get("_rdt_uuid"), "rdt_cid": ft.get("rdt_cid"),
    }.items() if v}


def queue(conn, event: str, event_id: str, ctx: dict | None, value: float | None = None,
          currency: str = "USD") -> int:
    """Queue SignUp / Purchase for every platform with a token. `ctx` None (consent, or no
    context kept) sends nothing. Returns rows queued; a repeat of the same event id is a no-op."""
    if event not in EVENTS or not pixels.clean_event_id(event_id) or not ctx:
        return 0
    payload = {"event": event, "event_id": event_id, "ts": int(time.time()), "ctx": ctx,
               "value": None if value is None else round(float(value), 2), "currency": (currency or "USD").upper()}
    n = 0
    for platform in enabled_platforms():
        try:
            n += conn.execute("INSERT OR IGNORE INTO conversion_queue (platform, event, event_id, payload, created_at) "
                              "VALUES (?,?,?,?,?)", (platform, event, event_id, json.dumps(payload), db.now())).rowcount
        except Exception as e:  # noqa: BLE001
            log.warning("conversion queue error: %s", str(e)[:120])
    conn.commit()
    return n


def build(platform: str, p: dict) -> tuple[str, dict, dict]:
    """(url, headers, json body) for one conversion. Pure: the tests check the shapes."""
    ctx = p.get("ctx") or {}
    name = pixels.EVENTS[platform][p["event"]]
    pixel = pixels.ids()[platform]
    value = p.get("value")
    url = ctx.get("url") or SITE
    if platform == "meta":
        ud = {k: v for k, v in {"client_ip_address": ctx.get("ip"), "client_user_agent": ctx.get("ua"),
                                "fbc": ctx.get("fbc"), "fbp": ctx.get("fbp")}.items() if v}
        ev = {"event_name": name, "event_time": p["ts"], "event_id": p["event_id"], "action_source": "website",
              "event_source_url": url, "user_data": ud}
        if value is not None:
            ev["custom_data"] = {"value": value, "currency": p.get("currency", "USD")}
        body = {"data": [ev]}
        if os.environ.get("META_TEST_EVENT_CODE"):
            body["test_event_code"] = os.environ["META_TEST_EVENT_CODE"]
        # as Crowdcut sends it; errors below never log the URL
        return (f"https://graph.facebook.com/v21.0/{pixel}/events?access_token={_token('meta')}",
                {"Content-Type": "application/json"}, body)
    if platform == "tiktok":
        user = {k: v for k, v in {"ip": ctx.get("ip"), "user_agent": ctx.get("ua"), "ttclid": ctx.get("ttclid"),
                                  "ttp": ctx.get("ttp")}.items() if v}
        ev = {"event": name, "event_time": p["ts"], "event_id": p["event_id"], "user": user, "page": {"url": url}}
        if value is not None:
            ev["properties"] = {"value": value, "currency": p.get("currency", "USD")}
        body = {"event_source": "web", "event_source_id": pixel, "data": [ev]}
        if os.environ.get("TIKTOK_TEST_EVENT_CODE"):
            body["test_event_code"] = os.environ["TIKTOK_TEST_EVENT_CODE"]
        return ("https://business-api.tiktok.com/open_api/v1.3/event/track/",
                {"Access-Token": _token("tiktok"), "Content-Type": "application/json"}, body)
    if platform == "reddit":
        user = {k: v for k, v in {"ip_address": hashlib.sha256(ctx["ip"].encode()).hexdigest() if ctx.get("ip") else None,
                                  "user_agent": ctx.get("ua"), "uuid": ctx.get("rdt_uuid")}.items() if v}
        meta = {"conversion_id": p["event_id"]}
        if value is not None:
            meta.update(value_decimal=value, currency=p.get("currency", "USD"))
        ev = {"event_at": datetime.fromtimestamp(p["ts"], tz=timezone.utc).isoformat(timespec="seconds"),
              "event_type": {"tracking_type": name}, "event_metadata": meta, "user": user}
        if ctx.get("rdt_cid"):
            ev["click_id"] = ctx["rdt_cid"]
        body = {"events": [ev]}
        if os.environ.get("REDDIT_CAPI_TEST", "").strip() in ("1", "true", "yes"):
            body["test_mode"] = True
        return (f"https://ads-api.reddit.com/api/v2.0/conversions/events/{pixel}",
                {"Authorization": f"Bearer {_token('reddit')}", "Content-Type": "application/json",
                 "User-Agent": "campaignify-capi/1.0"}, body)
    raise ValueError(platform)


def _send(platform: str, payload: dict) -> tuple[bool, str]:
    url, headers, body = build(platform, payload)
    try:
        r = requests.post(url, headers=headers, json=body, timeout=8)
    except requests.RequestException as e:
        return False, f"network: {type(e).__name__}"
    txt = re.sub(r"access_token=[^&\s\"]+", "access_token=…", (r.text or "")[:300])
    if r.status_code >= 400:
        return False, f"{r.status_code} {txt}"
    if platform == "tiktok":
        try:
            if r.json().get("code") not in (0, None):
                return False, f"tiktok {txt}"
        except ValueError:
            pass
    return True, txt


def flush(conn, limit: int = 25) -> dict:
    """Send what is pending (and retry failures, up to MAX_ATTEMPTS, for 3 days). Each row is
    claimed before sending so two processes never send it twice."""
    st = {"sent": 0, "failed": 0}
    since = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0).isoformat()
    rows = conn.execute("SELECT * FROM conversion_queue WHERE status IN ('pending','failed') AND attempts<? "
                        "AND created_at>? ORDER BY id LIMIT ?", (MAX_ATTEMPTS, since, limit)).fetchall()
    for r in rows:
        if r["platform"] not in enabled_platforms():
            continue
        cur = conn.execute("UPDATE conversion_queue SET status='sending', attempts=attempts+1 WHERE id=? "
                           "AND status IN ('pending','failed')", (r["id"],))
        conn.commit()
        if not cur.rowcount:
            continue
        try:
            ok, note = _send(r["platform"], json.loads(r["payload"]))
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"error: {type(e).__name__}"
        conn.execute("UPDATE conversion_queue SET status=?, error=?, sent_at=? WHERE id=?",
                     ("sent" if ok else "failed", None if ok else note[:300], db.now() if ok else None, r["id"]))
        conn.commit()
        st["sent" if ok else "failed"] += 1
    # a row left 'sending' by a crashed process goes back in the queue after 10 minutes
    conn.execute("UPDATE conversion_queue SET status='failed', error='interrupted' WHERE status='sending' AND created_at<?",
                 ((datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0).isoformat(),))
    conn.commit()
    return st


def flush_soon() -> None:
    """Send the queue on a daemon thread so the request that queued it returns at once."""
    if not enabled_platforms() or os.environ.get("RS_CAPI_SYNC") == "1":
        return

    def run():
        try:
            with db.connect() as c:
                flush(c)
        except Exception as e:  # noqa: BLE001
            log.warning("conversion flush error: %s", str(e)[:150])
    threading.Thread(target=run, daemon=True, name="capi-flush").start()


_last_tick = [0.0]
RETRY_SECONDS = 300


def tick() -> None:
    """Worker housekeeping, every few minutes: retry what didn't go out."""
    if not enabled_platforms() or time.monotonic() - _last_tick[0] < RETRY_SECONDS:
        return
    _last_tick[0] = time.monotonic()
    with db.connect() as c:
        flush(c)


# ---------------------------------------------------------------- the two moments

def on_signup(conn, user_id: int, ctx: dict | None) -> int:
    n = queue(conn, "SignUp", gads.event_id("sign_up", f"signup-{user_id}"), ctx)
    if n:
        flush_soon()
    return n


def on_checkout_paid(conn, sess: dict, workspace_id) -> int:
    """checkout.session.completed (paid) for a checkout we started (ad_conversions) in this
    workspace: Purchase at what Stripe charged. Webhook retries are no-ops (same event id)."""
    sid = str((sess or {}).get("id") or "")
    if not sid.startswith("cs_") or sess.get("payment_status") not in ("paid", "no_payment_required"):
        return 0
    if not enabled_platforms():
        return 0
    row = conn.execute("SELECT * FROM ad_conversions WHERE kind='purchase' AND ref=? AND workspace_id=?",
                       (sid, workspace_id)).fetchone()
    if not row or not row["ctx_json"]:
        return 0
    try:
        ctx = json.loads(row["ctx_json"])
    except ValueError:
        return 0
    cents = sess["amount_total"] if isinstance(sess.get("amount_total"), int) else row["value_cents"]
    n = queue(conn, "Purchase", gads.event_id("purchase", sid), ctx, value=(cents or 0) / 100,
              currency=sess.get("currency") or row["currency"] or "usd")
    if n:
        flush_soon()
    return n

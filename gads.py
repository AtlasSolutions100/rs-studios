"""Google Ads on our own site: the Google tag, conversions and first-touch attribution.

The tag (AW-18543041154, the owner's Google Ads account) loads on every page of
campaignify.ai that uses web/base.html: the public site and the app. Member pages
(/p/, /f/, /c/) and share links (/s/) never get it: they run no scripts.

Conversions fire only when their label is set (Google Ads > Goals > conversion
action > Tag setup shows "send_to": "AW-.../<label>"):

    GADS_SIGNUP_LABEL    sign_up: on the first page after a new account is created
    GADS_PURCHASE_LABEL  purchase: on the billing page Stripe returns to after a plan
                         or credit-pack checkout, with value + currency and the
                         Stripe Checkout Session id as transaction_id (and reported
                         once per session on our side too, ad_conversions table)
    GADS_TAG_ID          optional: another tag id, or "off" to drop the tag (local dev)

Consent: there's no cookie banner, so the tag starts with ad cookies denied in the
EEA, the UK and Switzerland (Google sends cookieless pings there) and for anyone whose
browser sends Global Privacy Control or Do Not Track; elsewhere they're granted.

The same two moments go to the Meta, TikTok, Reddit and X pixels when their ids are set
(pixels.py, web/_pixels.html), so the session queue below holds an event whenever Google or
any pixel wants it; each event carries an event id (signup-<user id>, purchase-<Checkout
Session id>) that the server-side copies (conversions.py) reuse for deduplication.

First touch: a visit to a public page carrying UTM tags or an ad click id
(gclid/gbraid/wbraid, fbclid, ttclid, rdt_cid, twclid) leaves a first-party cookie (`cfy_ft`,
90 days). The first UTM set is kept; a click id is added if the cookie has none yet. Sign-up copies it
onto the account (users.first_touch). Not set under GPC/DNT or for EEA/UK/CH
visitors when the edge tells us the country.
"""
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlencode

DEFAULT_TAG = "AW-18543041154"
LABEL_ENV = {"sign_up": "GADS_SIGNUP_LABEL", "purchase": "GADS_PURCHASE_LABEL"}
COOKIE = "cfy_ft"
COOKIE_DAYS = 90
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
CLICK_KEYS = ("gclid", "gbraid", "wbraid", "fbclid", "ttclid", "rdt_cid", "twclid")
SESSION_KEY = "gads_pending"
# Consent defaults to denied here (Google matches the visitor's region itself).
CONSENT_REGIONS = ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IT",
                   "LV", "LI", "LT", "LU", "MT", "NL", "NO", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "GB", "CH"]
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_VAL_RE = re.compile(r"[^\w.\-+~:/ ]")


def tag_id() -> str:
    v = (os.environ.get("GADS_TAG_ID") or DEFAULT_TAG).strip()
    return "" if v.lower() in ("off", "0", "none", "false") else v


def label(kind: str) -> str:
    """The conversion label for `kind`, or "" when unset. Accepts the bare label or
    the whole send_to value ("AW-123/AbC")."""
    raw = (os.environ.get(LABEL_ENV[kind]) or "").strip()
    if "/" in raw:
        raw = raw.rsplit("/", 1)[1]
    return raw if _LABEL_RE.match(raw) else ""


def send_to(kind: str) -> str:
    t, lab = tag_id(), label(kind)
    return f"{t}/{lab}" if t and lab else ""


# ---------------------------------------------------------------- conversions

def wanted(kind: str) -> bool:
    """Google (its label set) or any ad pixel (pixels.py) takes this conversion."""
    import pixels
    return bool(send_to(kind)) or pixels.enabled()


def queue(session, kind: str, **params):
    """Fire `kind` on the next page that renders the layout (only when Google's label or a pixel is set)."""
    if not wanted(kind):
        return False
    pending = list(session.get(SESSION_KEY) or [])[-4:]
    pending.append(dict(params, kind=kind))
    session[SESSION_KEY] = pending
    return True


def take(session) -> list[dict]:
    """The conversion events to fire on this page (each once): {"kind", "event_id", "value",
    "currency", "params"}, where params are the gtag 'conversion' parameters with send_to,
    or None when Google's label isn't set (the pixels still fire). Events nobody wants
    any more are dropped."""
    pending = session.pop(SESSION_KEY, None) or []
    out = []
    for ev in pending:
        kind = ev.get("kind") or ""
        if kind not in LABEL_ENV or not wanted(kind):
            continue
        st = send_to(kind)
        p = None
        if st:
            p = {"send_to": st}
            if ev.get("value") is not None:
                p["value"] = round(float(ev["value"]), 2)
                p["currency"] = (ev.get("currency") or "USD").upper()
            if ev.get("transaction_id"):
                p["transaction_id"] = str(ev["transaction_id"])[:100]
        out.append({"kind": kind, "params": p, "event_id": event_id(kind, ev.get("transaction_id")),
                    "value": None if ev.get("value") is None else round(float(ev["value"]), 2),
                    "currency": (ev.get("currency") or "USD").upper()})
    return out


def event_id(kind: str, ref) -> str:
    """The id every platform deduplicates on: signup-<user id> (already the sign_up
    transaction id) and purchase-<Checkout Session id>."""
    ref = str(ref or "")[:90]
    if not ref:
        return ""
    return ref if kind == "sign_up" else f"purchase-{ref}"


def record_checkout(conn, ref: str, workspace_id: int, user_id: int, value_cents: int, currency: str, now: str,
                    ctx: dict | None = None):
    """Remember a checkout we started so its return page can report it once. `ctx`
    (conversions.context) is the visitor's browser context for the server-side copy
    the Stripe webhook sends; only kept when a server-side token is set."""
    conn.execute("INSERT OR IGNORE INTO ad_conversions (kind, ref, workspace_id, user_id, value_cents, currency, "
                 "created_at, ctx_json) VALUES ('purchase',?,?,?,?,?,?,?)",
                 (ref, workspace_id, user_id, int(value_cents), (currency or "usd").lower(), now,
                  json.dumps(ctx) if ctx else None))


def claim_purchase(conn, ref: str, workspace_id: int, now: str, value_cents=None, currency=None):
    """The purchase conversion for Checkout Session `ref` of this workspace, once:
    {"value", "currency", "transaction_id"} or None (unknown, someone else's, already
    reported, or neither Google's purchase label nor a pixel set). `value_cents`/`currency`
    override what was recorded at checkout (e.g. Stripe's amount_total after a promotion code)."""
    if not ref or not wanted("purchase"):
        return None
    row = conn.execute("SELECT * FROM ad_conversions WHERE kind='purchase' AND ref=? AND workspace_id=? "
                       "AND reported_at IS NULL", (ref, workspace_id)).fetchone()
    if not row:
        return None
    if conn.execute("UPDATE ad_conversions SET reported_at=? WHERE id=? AND reported_at IS NULL",
                    (now, row["id"])).rowcount != 1:
        return None
    conn.commit()
    cents = row["value_cents"] if value_cents is None else value_cents
    return {"value": (cents or 0) / 100, "currency": (currency or row["currency"] or "usd").upper(),
            "transaction_id": ref}


# ---------------------------------------------------------------- first touch

def opted_out(headers) -> bool:
    """Global Privacy Control / Do Not Track, or a visitor the edge places in a consent region."""
    if headers.get("Sec-GPC", "").strip() == "1" or headers.get("DNT", "").strip() == "1":
        return True
    country = (headers.get("CF-IPCountry") or headers.get("X-Country") or "").strip().upper()
    return country in CONSENT_REGIONS


def _clean(v) -> str:
    return _VAL_RE.sub("", str(v or ""))[:120].strip()


def parse_cookie(raw: str | None) -> dict:
    if not raw:
        return {}
    keep = set(UTM_KEYS) | set(CLICK_KEYS) | {"lp", "ref", "at"}
    return {k: _clean(v) for k, v in parse_qsl(raw[:2000]) if k in keep and _clean(v)}


def touch(args, headers, path: str, current: str | None, referrer_host: str = "") -> str | None:
    """The new cookie value for a visit with these query args, or None to leave it.
    Keeps the first UTM set; adds a click id only when there's none yet."""
    have = parse_cookie(current)
    utm = {k: _clean(args.get(k)) for k in UTM_KEYS if _clean(args.get(k))}
    clicks = {k: _clean(args.get(k)) for k in CLICK_KEYS if _clean(args.get(k))}
    if not (utm or clicks) or opted_out(headers):
        return None
    if have:
        if any(k in have for k in CLICK_KEYS) or not clicks:
            return None
        new = dict(have, **clicks)
    else:
        new = dict(utm, **clicks, lp=_clean(path), at=str(int(time.time())))
        if referrer_host:
            new["ref"] = _clean(referrer_host)
    return urlencode(new)


def first_touch_json(raw: str | None) -> str | None:
    """What sign-up stores on the account (JSON), or None."""
    d = parse_cookie(raw)
    return json.dumps(d, sort_keys=True) if d else None

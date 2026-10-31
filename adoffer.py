"""The paid-ad sign-up offer: more starter credits for people who came from an ad.

SIGNUP_CREDITS_ADS (default 500) is how many starter credits a new self-serve
account gets when its first touch (the `cfy_ft` cookie, gads.py) shows a paid-ad
source: utm_medium cpc / ppc / cpm / paid (or paid_social, paid-search, ...) or an
ad click id (gclid, gbraid, wbraid, fbclid, ttclid, rdt_cid, twclid). "0" or "off"
gives everyone the normal starter credits (catalog.starter_credits(), 44) again.

One offer per person, on top of the sign-up IP throttles and the confirmed-email
rule for spending free credits (verify.py, #50):
  * once per normalised email address (case, +tags and Gmail dots, as the free
    preview), recorded in ad_signup_grants, which outlives deleted accounts;
  * at most IP_LIMIT grants per network (hashed IP) in IP_WINDOW_DAYS;
  * not again in a browser that already got it (the `cfy_adg` cookie);
  * at most SIGNUP_CREDITS_ADS_PER_DAY grants a day across all accounts (default 200).
Anyone who doesn't qualify gets the normal starter credits.

The page copy uses the same test (offer(), web templates' signup_offer()): a
visitor sees "Make your first video ad free" only when the grant would be made
for them and it covers the cheapest video ad (a 5-second product video);
everyone else sees "Try 2 image ads free".
"""
import os
from datetime import datetime, timedelta, timezone

import catalog
import gads
import preview

DEFAULT_CREDITS = 500
DEFAULT_PER_DAY = 200
IP_LIMIT = 3
IP_WINDOW_DAYS = 30
BROWSER_COOKIE = "cfy_adg"
BROWSER_COOKIE_DAYS = 365
PAID_MEDIUMS = {"cpc", "ppc", "cpm", "paid"}
AD_CLICK_KEYS = ("gclid", "gbraid", "wbraid", "fbclid", "ttclid", "rdt_cid", "twclid")
REASON = "starter credits (ad sign-up offer)"


def credits() -> int:
    """Starter credits for an ad-sourced sign-up, or 0 when the offer is off
    (SIGNUP_CREDITS_ADS "0"/"off", or not more than the normal starter credits)."""
    raw = os.environ.get("SIGNUP_CREDITS_ADS", "").strip().lower()
    if raw == "":
        n = DEFAULT_CREDITS
    elif raw.isdigit():
        n = int(raw)
    else:
        n = 0   # "off", "none", "false" or anything unreadable
    return n if n > catalog.starter_credits() else 0


def per_day() -> int:
    try:
        return int(os.environ.get("SIGNUP_CREDITS_ADS_PER_DAY", str(DEFAULT_PER_DAY)))
    except ValueError:
        return DEFAULT_PER_DAY


def paid_source(touch: dict) -> bool:
    """Does this first touch (gads.parse_cookie) come from a paid ad?"""
    if not touch:
        return False
    medium = (touch.get("utm_medium") or "").strip().lower()
    if medium in PAID_MEDIUMS or medium.startswith("paid"):
        return True
    return any(touch.get(k) for k in AD_CLICK_KEYS)


def effective_touch(request, first_touch_endpoints) -> dict:
    """The first touch this visitor has *after* this request: the cookie, or the
    value this very response will set (app._first_touch), so a landing from an ad
    shows the ad copy on its first page view."""
    current = request.cookies.get(gads.COOKIE)
    if request.method == "GET" and request.endpoint in first_touch_endpoints and request.args:
        new = gads.touch(request.args, request.headers, request.path, current)
        if new:
            return gads.parse_cookie(new)
    return gads.parse_cookie(current)


def _since(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def eligible(conn, touch: dict, ip: str, browser_marked: bool, email: str | None = None) -> bool:
    """Would a sign-up now get the ad offer? (`email` is known only at sign-up.)"""
    if not credits() or browser_marked or not paid_source(touch):
        return False
    if conn.execute("SELECT COUNT(*) FROM ad_signup_grants WHERE ip_hash=? AND created_at>=?",
                    (preview.ip_hash(ip), _since(IP_WINDOW_DAYS))).fetchone()[0] >= IP_LIMIT:
        return False
    if conn.execute("SELECT COUNT(*) FROM ad_signup_grants WHERE created_at>=?", (_since(1),)).fetchone()[0] >= per_day():
        return False
    if email is not None and conn.execute("SELECT 1 FROM ad_signup_grants WHERE email_norm=?",
                                          (preview.norm_email(email),)).fetchone():
        return False
    return True


def claim(conn, ws_id: int, email: str, ip: str, touch: dict, browser_marked: bool, now: str) -> int:
    """At sign-up: the starter credits this new workspace gets, recording the ad
    offer when it applies (no commit; the caller adds the credits)."""
    if not eligible(conn, touch, ip, browser_marked, email):
        return 0
    n = credits()
    cur = conn.execute("INSERT OR IGNORE INTO ad_signup_grants (workspace_id, email_norm, ip_hash, credits, created_at) "
                       "VALUES (?,?,?,?,?)", (ws_id, preview.norm_email(email), preview.ip_hash(ip), n, now))
    return n if cur.rowcount == 1 else 0


def offer(n_credits: int | None = None) -> dict:
    """The sign-up offer copy for `n_credits` starter credits (default: the normal
    ones): {"credits", "images", "video", "ad", "cta", "body"}."""
    r = catalog.rates()
    base = catalog.starter_credits()
    n = base if n_credits is None else n_credits
    images = n // max(r["image"], 1)
    video = n > base and n >= r["video5"]   # the cheapest video ad: a 5-second product video
    ads = f"{images} image ad{'' if images == 1 else 's'}"
    if video:
        cta = "Make your first video ad free"
        body = (f"Free sign-up includes {n:,} credits, enough for your first video ad (a 5-second product video) "
                f"or {ads}. No card needed.")
    else:
        cta = f"Try {ads} free"
        body = f"Free sign-up includes {ads} ({n:,} credits). No card needed."
    return {"credits": n, "images": images, "video": video, "ad": n > base, "cta": cta, "body": body}

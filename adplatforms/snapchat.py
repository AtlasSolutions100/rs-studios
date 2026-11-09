"""Snapchat adapter (roadmap M6.1): connect a workspace's own Snapchat ad account, launch a plan's 9:16 videos as a
PAUSED campaign, read its stats back. The Snapchat Marketing API (adsapi.snapchat.com/v1) with `requests` through
adhttp.call, the house style of meta.py and google.py.

Connect, read only:
    1. OAuth 2.0 authorization code flow at accounts.snapchat.com with the scope snapchat-marketing-api and a
       single-use state (Snap's Marketing API OAuth has no PKCE)
    2. code -> tokens          POST accounts.snapchat.com/login/oauth2/access_token. The refresh token (it does
                               not expire or rotate, research B4) goes straight into the token box and is the only
                               credential kept; access tokens (3,600 s) are minted from it per run and live in this
                               process only
    3. who connected           GET /v1/me
    4. what can be picked      GET /v1/me/organizations?with_ad_accounts=true (the ad accounts of each
                               organization), each organization's public profiles (every Snap ad runs from one), and
                               after the pick GET /v1/adaccounts/<id>/pixels
Snap has no endpoint to revoke a grant: Disconnect deletes the token here and says where to remove the app on Snap.

Launch: every object is PAUSED, made after the owner approved the card that lists it, in this order (the same
saga as Meta, launches.py): upload each video (POST .../media, then POST /v1/media/<id>/upload), one WEB_VIEW
creative per video (top snap = the video, headline, call to action, the landing page, the public profile), the
campaign, one ad squad (the daily budget, where, ages, gender, automatic placements, the optimization goal and the
pixel), one ad per creative. Every name ends in "[cfy:<launch id>:<step>]": a step that may have run already (a
lost answer, a crash) finds its object by listing, instead of making a second one. A failed launch deletes its ads;
creatives (Snap can't delete them) and media stay unused in the account; the campaign and ad squad stay PAUSED.

Stats: GET /v1/campaigns/<id>/stats with granularity DAY (spend in micro-currency), as rows shaped like the design
doc's ad_insights_daily, so the insights job (M2.5) can store them for every platform.

Field names and values as documented on developers.snap.com/marketing-api (quick start, campaigns and ad squads
read 2026-11-09). Not yet run against a live account: tools/snapchat_sandbox_launch.py does that and records the
answers into tests/fixtures/snapchat/recorded/. Points to confirm there are marked "sandbox:".

Settings (GitHub secrets / variables, passed by deploy.yml):
    SNAPCHAT_ADS             the flag: 1 turns Snapchat connect and launch on (default off), on top of AD_LAUNCH
    SNAPCHAT_CLIENT_ID       the OAuth app's client id (Business Manager > Business Details > OAuth Apps)
    SNAPCHAT_CLIENT_SECRET   its client secret (shown once when the app is made)
    TOKEN_KEY                the token box key (secretsbox.py)
The OAuth app's redirect URI must be exactly RS_PUBLIC_URL + /connect/snapchat/callback, i.e.
https://campaignify.ai/connect/snapchat/callback in production.
"""
import hashlib
import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import adhttp
import adspec
import secretsbox
from adplatforms.meta import NotReady

API = "https://adsapi.snapchat.com/v1"
AUTH_URL = "https://accounts.snapchat.com/login/oauth2/authorize"
TOKEN_URL = "https://accounts.snapchat.com/login/oauth2/access_token"
SCOPES = ("snapchat-marketing-api",)
MAX_PAGES = 5                 # result pages we follow per listing
CREATE_RETRIES = 1            # adhttp attempts for a create; the saga looks up, then tries again
VIDEO_MAX_BYTES = 32 * 1024 * 1024     # the one-request upload (bigger files need Snap's chunked upload)
ADS_MANAGER = "https://ads.snapchat.com/"

# "No Snapchat ad account yet?" (decision D1: the member makes it in Snap's UI, billing included).
CHECKLIST = [
    ("Create the ad account", "Sign up in Snapchat Ads Manager with your business details. Pick the currency and time "
     "zone carefully: they can't be changed later.", "https://ads.snapchat.com/"),
    ("Add a payment method", "In Ads Manager, open Payment Methods and add a card. Ads can't run without one.",
     "https://ads.snapchat.com/"),
    ("Make a public profile", "Every Snapchat ad shows the name and logo of a public profile. Make one for the business "
     "in Ads Manager if you have none.", "https://businesshelp.snapchat.com/"),
    ("Install the Snap Pixel", "Needed to optimize for purchases or sign-ups. Traffic campaigns run without it.",
     "https://businesshelp.snapchat.com/s/article/pixel-website-install"),
]

OBJECTIVES = {"sales": "SALES", "leads": "LEADS", "traffic": "TRAFFIC", "awareness": "AWARENESS_AND_ENGAGEMENT"}
GOALS = {"traffic": "SWIPES", "awareness": "IMPRESSIONS"}
EVENT_GOALS = {"Purchase": "PIXEL_PURCHASE", "Lead": "PIXEL_SIGNUP", "CompleteRegistration": "PIXEL_SIGNUP",
               "AddToCart": "PIXEL_ADD_TO_CART", "ViewContent": "PIXEL_PAGE_VIEW"}
GOAL_WORDS = {"SWIPES": "swipe ups", "IMPRESSIONS": "impressions", "PIXEL_PURCHASE": "purchases",
              "PIXEL_SIGNUP": "sign-ups", "PIXEL_ADD_TO_CART": "adds to cart", "PIXEL_PAGE_VIEW": "page views"}
# Campaignify's buttons -> Snap's call_to_action values for a web view (sandbox: confirm each one is taken).
CTAS = {"SHOP_NOW": "SHOP_NOW", "LEARN_MORE": "LEARN_MORE", "SIGN_UP": "SIGN_UP", "BOOK_NOW": "BOOK_NOW",
        "ORDER_NOW": "ORDER_NOW", "APPLY_NOW": "APPLY_NOW", "DOWNLOAD": "DOWNLOAD", "SUBSCRIBE": "SUBSCRIBE",
        "GET_OFFER": "GET_NOW", "GET_QUOTE": "GET_NOW", "CONTACT_US": "CONTACT_US", "CALL_NOW": "CALL"}
OLDEST = 50                   # Snap's oldest age band is "50 and over": ages from 50 up send no max_age
UTM = "utm_source=snapchat&utm_medium=paid&utm_campaign={{campaign.id}}&utm_content={{ad.id}}"
ACCOUNT_OK = ("ACTIVE",)


def flag_on() -> bool:
    """SNAPCHAT_ADS=1 switches Snapchat connect and launch on (default off)."""
    return (os.environ.get("SNAPCHAT_ADS") or "").strip().lower() in ("1", "on", "true", "yes")


def enabled(ws, user=None) -> bool:
    """Snapchat shows only with both flags: AD_LAUNCH for this workspace (adplatforms.enabled) and SNAPCHAT_ADS."""
    import adplatforms
    return flag_on() and adplatforms.enabled(ws, user)


def client_id() -> str:
    return (os.environ.get("SNAPCHAT_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.environ.get("SNAPCHAT_CLIENT_SECRET") or "").strip()


def redirect_uri() -> str:
    import verify
    return verify.public_url() + "/connect/snapchat/callback"


def checklist():
    return list(CHECKLIST)


def ads_url(account_id=None) -> str:
    return ADS_MANAGER


# ---------------------------------------------------------------- access tokens (memory only)

_ACCESS = {}                 # sha256(refresh token) -> (access token, expires epoch)
_LOCK = threading.Lock()


def access_token(refresh_token: str) -> str:
    """A fresh access token for a stored refresh token (cached until a minute before it ends). A revoked grant
    answers invalid_grant: adhttp.TokenInvalid, so the connection needs Reconnect."""
    k = hashlib.sha256(refresh_token.encode()).hexdigest()
    now = adhttp.CLOCK()
    with _LOCK:
        hit = _ACCESS.get(k)
        if hit and hit[1] - 60 > now:
            return hit[0]
    body = adhttp.call("snapchat", "POST", TOKEN_URL, retries=2, secrets=[refresh_token, client_secret()],
                       data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id(),
                             "client_secret": client_secret()})
    tok = body.get("access_token")
    if not tok:
        raise adhttp.AdHttpError("snapchat", "no access_token in the refresh answer")
    with _LOCK:
        _ACCESS[k] = (tok, now + int(body.get("expires_in") or 1800))
    return tok


def forget_access(refresh_token: str):
    with _LOCK:
        _ACCESS.pop(hashlib.sha256(refresh_token.encode()).hexdigest(), None)


def _call(method: str, url: str, access: str, *, body=None, params=None, files=None, data=None, account=None,
          retries=None, max_wait=0.0):
    return adhttp.call("snapchat", method, url if url.startswith("https://") else API + url, params=params,
                       headers={"Authorization": f"Bearer {access}"}, json_body=body, files=files, data=data,
                       account=account, retries=retries, secrets=[access], max_wait=max_wait)


def items(body: dict, plural: str, singular: str) -> list:
    """The objects of a Snap answer: {"campaigns": [{"sub_request_status": "SUCCESS", "campaign": {...}}]}."""
    out = []
    for it in (body or {}).get(plural) or []:
        if isinstance(it, dict) and str(it.get("sub_request_status") or "SUCCESS").upper() == "SUCCESS" \
                and isinstance(it.get(singular), dict):
            out.append(it[singular])
    return out


def listing(path: str, access: str, plural: str, singular: str, account=None, params=None, max_wait=0.0) -> list:
    """Every object of a Snap list, following paging.next_link."""
    out, url = [], path
    q = dict(params or {}, limit=200) if "limit" not in (params or {}) else dict(params)
    for _ in range(MAX_PAGES):
        body = _call("GET", url, access, params=q, account=account, max_wait=max_wait)
        out += items(body, plural, singular)
        url = ((body.get("paging") or {}).get("next_link") or "")
        q = None
        if not url.startswith(API):
            break
    return out


def _first(body: dict, plural: str, singular: str, what: str) -> dict:
    got = items(body, plural, singular)
    if not got or not got[0].get("id"):
        raise adhttp.AdHttpError("snapchat", f"no {what} in the answer",
                                 user_message=adhttp._snap_reason(body) or "")
    return got[0]


class Snapchat:
    key = "snapchat"
    name = "Snapchat"

    def configured(self) -> bool:
        return bool(client_id() and client_secret()) and secretsbox.configured()

    def oauth_start(self, state: str, code_challenge: str = None) -> str:
        q = {"client_id": client_id(), "redirect_uri": redirect_uri(), "response_type": "code",
             "scope": " ".join(SCOPES), "state": state}
        return AUTH_URL + "?" + urlencode(q)

    def oauth_finish(self, code: str, code_verifier: str = None) -> dict:
        """Everything a pending connection needs: the refresh token (for the box), who connected and the ad
        accounts and public profiles that can be picked."""
        body = adhttp.call("snapchat", "POST", TOKEN_URL, retries=1, secrets=[code, client_secret()],
                           data={"grant_type": "authorization_code", "code": code, "client_id": client_id(),
                                 "client_secret": client_secret(), "redirect_uri": redirect_uri()})
        access, refresh = body.get("access_token"), body.get("refresh_token")
        if not access:
            raise adhttp.AdHttpError("snapchat", "no access_token in the code exchange")
        if not refresh:
            raise adhttp.AdHttpError("snapchat", "no refresh_token in the code exchange", code="oauth.no_refresh",
                                     user_message="Snapchat didn't send a lasting permission. Connect again.")
        scopes = sorted(set(str(body.get("scope") or " ".join(SCOPES)).replace(",", " ").split()))
        with _LOCK:
            _ACCESS[hashlib.sha256(refresh.encode()).hexdigest()] = (access, adhttp.CLOCK() + int(body.get("expires_in") or 1800))
        me = _call("GET", "/me", access).get("me") or {}
        accounts, orgs = self.list_accounts(access)
        return {"token": refresh, "expires_at": None, "scopes": scopes, "token_type": "REFRESH",
                "user_id": str(me.get("id") or ""), "user_name": str(me.get("display_name") or me.get("email") or "")[:80],
                "accounts": accounts, "pages": self.list_profiles(access, orgs), "missing": []}

    def list_accounts(self, access: str) -> tuple:
        """([{"id", "name", "currency", "timezone", "status", "usable", "org_id", "org_name"}], [organization ids])."""
        body = _call("GET", "/me/organizations", access, params={"with_ad_accounts": "true"})
        out, orgs = [], []
        for org in items(body, "organizations", "organization"):
            oid = str(org.get("id") or "")
            if not oid:
                continue
            orgs.append(oid)
            for a in org.get("ad_accounts") or []:
                aid = str(a.get("id") or "")
                if not aid:
                    continue
                st = str(a.get("status") or "").upper()
                out.append({"id": aid, "name": (a.get("name") or "Snapchat ad account")[:120],
                            "currency": a.get("currency") or "", "timezone": a.get("timezone") or "",
                            "status": "Active" if st in ACCOUNT_OK else (st.title() or "Unknown status"),
                            "usable": st in ACCOUNT_OK, "org_id": oid, "org_name": (org.get("name") or "")[:120]})
        out.sort(key=lambda a: (not a["usable"], a["org_name"].lower(), a["name"].lower()))
        return out, orgs

    def list_profiles(self, access: str, orgs) -> list:
        """The public profiles of each organization (sandbox: confirm the path). A missing permission or none:
        the picker says to make one in Ads Manager."""
        out = []
        for oid in orgs[:20]:
            try:
                rows = listing(f"/organizations/{oid}/public_profiles", access, "public_profiles", "public_profile")
            except adhttp.TokenInvalid:
                raise
            except adhttp.AdHttpError:
                continue
            for p in rows:
                if p.get("id"):
                    out.append({"id": str(p["id"]), "name": (p.get("display_name") or p.get("name") or "Public profile")[:80],
                                "org_id": oid})
        return out

    def list_pages(self, token: str) -> list:
        return []

    def list_pixels(self, token: str, account_id: str) -> list:
        """The ad account's Snap Pixels. `token` is the stored refresh token."""
        rows = listing(f"/adaccounts/{account_id}/pixels", access_token(token), "pixels", "pixel", account=str(account_id))
        return [{"id": str(r["id"]), "name": (r.get("name") or f"Pixel {r['id']}")[:120]} for r in rows if r.get("id")]

    def revoke(self, token: str) -> bool:
        """Snap has no endpoint to revoke a grant: the token is deleted here and the member removes the app on Snap."""
        forget_access(token)
        return False

    # ------------------------------------------------------------ launch, everything PAUSED

    def plan_steps(self, spec, conn_row, assets: dict) -> list:
        return plan_steps(spec, conn_row, assets)

    def execute_step(self, step: dict, ctx: dict) -> dict:
        return execute_step(step, ctx)

    def compensate(self, step: dict, ctx: dict) -> str:
        return compensate(step, ctx)

    def verify(self, step: dict, ctx: dict) -> bool:
        return verify(step, ctx)

    def upload_asset(self, ctx: dict, step: dict) -> dict:
        return _upload(step, ctx)

    def pull_insights(self, ctx: dict, objects, day_range) -> list:
        return pull_insights(ctx, objects, day_range)

    def _later(self, *a, **kw):
        raise NotImplementedError("manage for Snapchat arrives after M6")

    validate = set_status = update_budget = _later


ADAPTER = Snapchat()


# ================================================================ launch steps

def tag(launch_id, key: str) -> str:
    return f"[cfy:{launch_id}:{key}]"


def external_name(name: str, launch_id, key: str) -> str:
    return f"{(name or 'Campaignify')[:180].strip()} {tag(int(launch_id), key)}"


def micro(minor: int, currency: str) -> int:
    """A budget in minor units as Snap's micro-currency (1.00 = 1,000,000)."""
    return int(minor) * (1_000_000 // adspec.minor_factor(currency))


def _tz(name):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(name) if name else timezone.utc
    except Exception:  # noqa: BLE001  an unknown zone name: UTC
        return timezone.utc


def when(iso, tz_name, end=False):
    """A spec date as Snap's start_time / end_time: midnight (or the last second) in the ad account's time zone."""
    if not iso:
        return None
    d = date.fromisoformat(str(iso)[:10])
    t = datetime(d.year, d.month, d.day, 23, 59, 59) if end else datetime(d.year, d.month, d.day)
    return t.replace(tzinfo=_tz(tz_name)).isoformat()


def targeting(spec) -> dict:
    geo, aud = spec.geo or {}, spec.audience or {}
    demo = {"min_age": str(max(13, int(aud.get("age_min", 18))))}
    hi = int(aud.get("age_max", 65))
    if hi < OLDEST:
        demo["max_age"] = str(hi)
    g = (aud.get("genders") or ["all"])[0]
    if g in ("women", "men"):
        demo["gender"] = "FEMALE" if g == "women" else "MALE"
    return {"geos": [{"country_code": str(c).lower()} for c in geo.get("countries") or []], "demographics": [demo]}


def goal(spec) -> str:
    if spec.objective in ("sales", "leads") and (spec.conversion or {}).get("pixel_id"):
        return EVENT_GOALS.get((spec.conversion or {}).get("event") or ("Purchase" if spec.objective == "sales"
                                                                         else "Lead"), "PIXEL_SIGNUP")
    return GOALS.get(spec.objective, "SWIPES")


def landing(c) -> str:
    url = c.destination_url or ""
    if c.utm:
        url += ("&" if "?" in url else "?") + c.utm
    return url


def plan_steps(spec, conn_row, assets: dict) -> list:
    """The exact calls for a CampaignSpec: uploads, creatives, the campaign, the ad squad, the ads. "$<key>" in a
    request is filled at run time from an earlier step's result. No call is made here."""
    account = str(conn_row["external_account_id"])
    tz = conn_row["timezone"]
    steps = []
    for i, c in enumerate(spec.creatives, start=1):
        a = assets.get(c.asset_id) or {}
        steps.append({"key": f"upload:{i}", "kind": "upload", "name": f"{spec.name} · ad {i} video",
                      "request": {"asset_id": c.asset_id, "media": "video", "path": a.get("path")}})
    for i, c in enumerate(spec.creatives, start=1):
        cr = {"ad_account_id": account, "type": "WEB_VIEW", "top_snap_media_id": f"$upload:{i}",
              "headline": c.headline, "shareable": True, "call_to_action": CTAS.get(c.cta, "LEARN_MORE"),
              "web_view_properties": {"url": landing(c), "allow_snap_javascript_sdk": False, "block_preload": True}}
        if conn_row["page_id"]:
            cr["profile_properties"] = {"profile_id": str(conn_row["page_id"])}
        steps.append({"key": f"creative:{i}", "kind": "creative", "name": f"{spec.name} · ad {i} {c.name}".strip(),
                      "request": {"creative": cr}})
    camp = {"ad_account_id": account, "status": "PAUSED", "buy_model": "AUCTION",
            "start_time": when(spec.start_at, tz) or when(date.today().isoformat(), tz),
            "objective_v2_properties": {"objective_v2_type": OBJECTIVES[spec.objective]}}
    if spec.end_at:
        camp["end_time"] = when(spec.end_at, tz, end=True)
    steps.append({"key": "campaign", "kind": "campaign", "name": spec.name, "request": {"campaign": camp}})
    sq = {"campaign_id": "$campaign", "type": "SNAP_ADS", "status": "PAUSED", "placement_v2": {"config": "AUTOMATIC"},
          "billing_event": "IMPRESSION", "optimization_goal": goal(spec), "bid_strategy": "AUTO_BID",
          "delivery_constraint": "DAILY_BUDGET", "daily_budget_micro": micro(spec.daily_budget_minor, spec.currency),
          "targeting": targeting(spec), "start_time": camp["start_time"]}
    if spec.end_at:
        sq["end_time"] = camp["end_time"]
    if spec.objective in ("sales", "leads") and (spec.conversion or {}).get("pixel_id"):
        sq["pixel_id"] = str(spec.conversion["pixel_id"])
    steps.append({"key": "adset", "kind": "adsquad", "name": f"{spec.name} · ad squad", "request": {"adsquad": sq}})
    for i, c in enumerate(spec.creatives, start=1):
        steps.append({"key": f"ad:{i}", "kind": "ad", "name": f"{spec.name} · ad {i} {c.name}".strip(),
                      "request": {"ad": {"ad_squad_id": "$adset", "creative_id": f"$creative:{i}",
                                         "type": "REMOTE_WEBPAGE", "status": "PAUSED"}}})
    return steps


def _fill(value, results: dict):
    """"$key" -> that earlier step's id."""
    if isinstance(value, dict):
        return {k: _fill(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, results) for v in value]
    if isinstance(value, str) and value.startswith("$"):
        r = results.get(value[1:]) or {}
        if not r.get("id"):
            raise KeyError(f"step {value[1:]} has no result yet")
        return str(r["id"])
    return value


# kind -> (list path, create path, plural, singular); "{a}" the ad account, "{p}" the parent's id
EDGES = {"campaign": ("/adaccounts/{a}/campaigns", "/adaccounts/{a}/campaigns", "campaigns", "campaign"),
         "adsquad": ("/campaigns/{p}/adsquads", "/campaigns/{p}/adsquads", "adsquads", "adsquad"),
         "creative": ("/adaccounts/{a}/creatives", "/adaccounts/{a}/creatives", "creatives", "creative"),
         "ad": ("/adsquads/{p}/ads", "/adsquads/{p}/ads", "ads", "ad"),
         "media": ("/adaccounts/{a}/media", "/adaccounts/{a}/media", "media", "media")}
PARENT = {"adsquad": "campaign_id", "ad": "ad_squad_id"}


def _ctx_access(ctx: dict) -> str:
    return access_token(ctx["token"])


def find_tagged(kind: str, ctx: dict, key: str, access: str, parent: str = None):
    """The object of this launch step already on Snap (a create whose answer was lost), or None."""
    t = tag(ctx["launch_id"], key)
    lst, _, plural, singular = EDGES[kind]
    if "{p}" in lst and not parent:
        return None
    rows = listing(lst.format(a=ctx["account"], p=parent), access, plural, singular, account=str(ctx["account"]),
                   max_wait=float(ctx.get("max_wait") or 0))
    for r in rows:
        if str(r.get("name") or "").endswith(t) and str(r.get("status") or "") != "DELETED" and not r.get("deleted"):
            return r
    return None


def _create(kind: str, obj: dict, ctx: dict, access: str) -> dict:
    _, path, plural, singular = EDGES[kind]
    parent = obj.get(PARENT.get(kind, ""), "")
    body = _call("POST", path.format(a=ctx["account"], p=parent), access, body={plural: [obj]},
                 account=str(ctx["account"]), retries=CREATE_RETRIES, max_wait=float(ctx.get("max_wait") or 0))
    return _first(body, plural, singular, kind)


def _upload(step: dict, ctx: dict) -> dict:
    """Create the media object, then upload the file to it. A re-run finds the media by its tagged name and uploads
    only if Snap is still waiting for the file."""
    req, access, acct = step["request"], _ctx_access(ctx), str(ctx["account"])
    name = external_name(step["name"], ctx["launch_id"], step["key"])
    found = find_tagged("media", ctx, step["key"], access) if ctx.get("lookup") else None
    media = found or _create("media", {"name": name, "type": "VIDEO", "ad_account_id": acct}, ctx, access)
    if str(media.get("media_status") or "PENDING_UPLOAD").upper() == "PENDING_UPLOAD":
        with open(req["path"], "rb") as f:
            _call("POST", f"/media/{media['id']}/upload", access, account=acct, retries=CREATE_RETRIES,
                  files={"file": (os.path.basename(req["path"]), f, "video/mp4")},
                  max_wait=float(ctx.get("max_wait") or 0))
    return {"id": str(media["id"]), "object": "video", **({"found": True} if found else {})}


def _media_ready(media_id: str, ctx: dict, access: str):
    body = _call("GET", f"/media/{media_id}", access, account=str(ctx["account"]))
    m = (items(body, "media", "media") or [{}])[0]
    st = str(m.get("media_status") or "").upper()
    if st in ("READY", ""):
        return
    if st in ("ERROR", "FAILED", "UPLOAD_FAILED"):
        raise adhttp.AdHttpError("snapchat", f"media {media_id} failed processing on Snapchat",
                                 user_message="Snapchat couldn't process the video. Try another export of it.")
    raise NotReady(15.0)


def execute_step(step: dict, ctx: dict) -> dict:
    """Run one step. ctx: token (the refresh token), account (ad account id), launch_id, results, lookup. Returns
    {"id", ...}; raises adhttp errors or NotReady (a video still processing)."""
    kind, key, results = step["kind"], step["key"], ctx["results"]
    if kind == "upload":
        return _upload(step, ctx)
    access = _ctx_access(ctx)
    obj = _fill(dict(step["request"][kind]), results)
    if kind == "creative":
        _media_ready(obj["top_snap_media_id"], ctx, access)
    if ctx.get("lookup"):
        found = find_tagged(kind, ctx, key, access, obj.get(PARENT.get(kind, "")))
        if found:
            return {"id": str(found["id"]), "found": True}
    obj["name"] = external_name(step["name"], ctx["launch_id"], key)
    made = _create(kind, obj, ctx, access)
    return {"id": str(made["id"]), "status": made.get("status")}


READONLY = ("created_at", "updated_at", "delivery_status", "creation_state", "review_status", "review_status_reasons",
            "pacing_properties_version", "delivery_properties_version")


def _read(kind: str, oid: str, ctx: dict, access: str, max_wait: float = 0.0):
    plural, singular = EDGES[kind][2], EDGES[kind][3]
    path = {"campaign": f"/campaigns/{oid}", "adsquad": f"/adsquads/{oid}", "ad": f"/ads/{oid}",
            "creative": f"/creatives/{oid}", "media": f"/media/{oid}"}[kind]
    got = items(_call("GET", path, access, account=str(ctx["account"]), max_wait=max_wait), plural, singular)
    return got[0] if got else None


def _pause(kind: str, obj: dict, ctx: dict, access: str, max_wait: float):
    """Snap updates take the whole object (PUT): send it back as read, with status PAUSED."""
    o = {k: v for k, v in obj.items() if k not in READONLY}
    o["status"] = "PAUSED"
    path = "/adaccounts/{a}/campaigns" if kind == "campaign" else "/campaigns/{p}/adsquads"
    _call("PUT", path.format(a=ctx["account"], p=o.get("campaign_id", "")), access,
          body={EDGES[kind][2]: [o]}, account=str(ctx["account"]), max_wait=max_wait)


def compensate(step: dict, ctx: dict) -> str:
    """Undo one done step of a failed launch: ads are deleted ("compensated"); the campaign and ad squad stay PAUSED
    and are reported ("kept"); creatives and media stay unused in the account (Snap can't delete creatives)."""
    kind, ext = step["kind"], step.get("external_id")
    wait = float(ctx.get("max_wait") or 0)
    if not ext:
        return "compensated"
    access = _ctx_access(ctx)
    if kind == "ad":
        _call("DELETE", f"/ads/{ext}", access, account=str(ctx["account"]), max_wait=wait)
        return "compensated"
    if kind in ("campaign", "adsquad"):
        cur = _read(kind, ext, ctx, access, wait)
        if cur and str(cur.get("status") or "PAUSED") != "PAUSED":
            _pause(kind, cur, ctx, access, wait)
        return "kept"
    return "kept"


def verify(step: dict, ctx: dict) -> bool:
    """A kept object of an earlier run is still on Snap (before a retry reuses it)."""
    kind, ext = step["kind"], step.get("external_id")
    if not ext:
        return False
    kind = "media" if kind == "upload" else kind
    try:
        obj = _read(kind, ext, ctx, _ctx_access(ctx))
    except adhttp.TokenInvalid:
        raise
    except adhttp.AdHttpError as e:
        if e.status in (400, 404):
            return False
        raise
    return bool(obj) and str(obj.get("status") or "") != "DELETED" and not obj.get("deleted")


# ================================================================ stats

STAT_FIELDS = "impressions,swipes,spend,conversion_purchases,conversion_sign_ups,conversion_purchases_value"


def pull_insights(ctx: dict, objects, day_range) -> list:
    """Daily stats for the launched campaigns: [{"platform", "external_object_id", "level", "day", "spend_minor",
    "currency", "impressions", "clicks", "link_clicks", "leads", "purchases", "purchase_value_minor"}].
    ctx: token, account, currency, timezone. objects: [{"kind": "campaign", "external_id"}]. day_range: (first,
    last) dates; Snap wants whole days at midnight in the account's time zone."""
    access = _ctx_access(ctx)
    first, last = (date.fromisoformat(str(d)[:10]) for d in day_range)
    tz = ctx.get("timezone")
    start = when(first.isoformat(), tz)
    end = datetime.fromisoformat(when((last + timedelta(days=1)).isoformat(), tz)).isoformat()
    cur = ctx.get("currency") or "USD"
    per_minor = 1_000_000 // adspec.minor_factor(cur)
    out = []
    for o in objects:
        if o.get("kind") != "campaign" or not o.get("external_id"):
            continue
        body = _call("GET", f"/campaigns/{o['external_id']}/stats", access, account=str(ctx["account"]),
                     params={"granularity": "DAY", "fields": STAT_FIELDS, "start_time": start, "end_time": end})
        for ts in items(body, "timeseries_stats", "timeseries_stat"):
            for row in ts.get("timeseries") or []:
                st = row.get("stats") or {}
                out.append({"platform": "snapchat", "external_object_id": str(ts.get("id") or o["external_id"]),
                            "level": "campaign", "day": str(row.get("start_time") or "")[:10],
                            "spend_minor": int(st.get("spend") or 0) // per_minor, "currency": cur,
                            "impressions": int(st.get("impressions") or 0), "clicks": int(st.get("swipes") or 0),
                            "link_clicks": int(st.get("swipes") or 0), "leads": int(st.get("conversion_sign_ups") or 0),
                            "purchases": int(st.get("conversion_purchases") or 0),
                            "purchase_value_minor": int(st.get("conversion_purchases_value") or 0) // per_minor,
                            "raw_json": json.dumps(st, separators=(",", ":"))[:2000]})
    return out


# ================================================================ errors in plain words

RECONNECT = "Snapchat ended Campaignify's access to this ad account. Reconnect it in Settings > Ad accounts, then try again."
NETWORK_WORDS = "We couldn't reach Snapchat. Try again in a few minutes."
WAITED_WORDS = "Snapchat kept asking us to slow down for over 2 hours. Try again later."
PERMISSION = ("This Snapchat login can't make campaigns in that ad account. Ask an admin of the ad account to give you "
              "the Account Manager role in Business Manager, then reconnect.")


def translate(err) -> str:
    if isinstance(err, adhttp.NetworkError):
        return NETWORK_WORDS
    if isinstance(err, adhttp.TokenInvalid):
        return RECONNECT
    if isinstance(err, adhttp.PermissionDenied):
        return PERMISSION
    if err.user_message:
        return f"Snapchat said: {err.user_message[:300]}"
    return "Snapchat refused part of the campaign. The details are below."


def retryable(err) -> bool:
    return isinstance(err, adhttp.AdHttpError) and (err.status or 0) >= 500 \
        and not isinstance(err, (adhttp.TokenInvalid, adhttp.PermissionDenied))

"""TikTok adapter (roadmap M6.2): connect a workspace's own TikTok advertiser account, launch a plan's videos as a PAUSED
campaign under a custom identity, read its numbers back. The TikTok API for Business v1.3 (business-api.tiktok.com
/open_api/v1.3) with `requests` through adhttp.call, the house style of meta.py and google.py. Spark Ads come later.

Connect:
    1. advertiser authorization: business-api.tiktok.com/portal/auth?app_id&state&redirect_uri; TikTok sends the
       person back with auth_code and state (single use, tied to the person; TikTok's flow has no PKCE)
    2. auth_code -> token      POST /oauth2/access_token/ {app_id, secret, auth_code}. The access token does not expire
                               until revoked (research B3) and goes straight into the token box; the advertiser ids
                               the person shared come with it
    3. who connected           GET /user/info/
    4. what can be picked      GET /oauth2/advertiser/get/ and /advertiser/info/ (name, currency, time zone, status);
                               after the pick, the advertiser's custom identities (GET /identity/get/) and pixels
                               (GET /pixel/list/). The identity is the name and avatar the ads show: the member picks
                               one or Campaignify makes one (POST /identity/create/, the only write of the connect)
Disconnect revokes the token at TikTok (POST /oauth2/revoke_token/).

Launch: the campaign is made turned off (operation_status DISABLE, "PAUSED" on our side), after the owner approved the
card that lists it, in this order (the saga in launches.py): upload each video and its cover image, the campaign, one
ad group (daily budget, countries, age bands, gender, the TikTok placement, the goal and the pixel), one ad per video
(identity, ad text, button, landing page). The ad group and ads are on inside the turned-off campaign, as on Google:
turning the campaign on is all it takes, and nothing serves before that. Every name ends in "[cfy:<launch id>:<step>]":
a step that may have run already finds its object by listing instead of making a second one. A failed launch deletes
its ads; the campaign and ad group stay off and are reported; uploads stay in the account's library.

Reporting: GET /report/integrated/get/ (BASIC, campaign by day) as rows shaped like the design doc's ad_insights_daily.

Field names and values as in TikTok's v1.3 API reference and its official SDK models (CampaignCreateBody,
AdgroupCreateBody, AdCreateBody; research B3), not yet run against a live advertiser: tools/tiktok_sandbox_launch.py
does that on the sandbox advertiser and records the answers into tests/fixtures/tiktok/recorded/. Points to confirm
there are marked "sandbox:".

Settings (GitHub secrets / variables, passed by deploy.yml):
    TIKTOK_ADS          the flag: 1 turns TikTok connect and launch on (default off), on top of AD_LAUNCH
    TIKTOK_APP_ID       the TikTok for Business developer app's App ID
    TIKTOK_APP_SECRET   its Secret
    TOKEN_KEY           the token box key (secretsbox.py)
The app's Advertiser redirect URL must be exactly RS_PUBLIC_URL + /connect/tiktok/callback, i.e.
https://campaignify.ai/connect/tiktok/callback in production.
"""
import hashlib
import json
import os
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode

import adhttp
import adspec
import secretsbox
from adplatforms.meta import NotReady

API = "https://business-api.tiktok.com/open_api/v1.3"
AUTH_URL = "https://business-api.tiktok.com/portal/auth"
ADS_MANAGER = "https://ads.tiktok.com/i18n/perf/campaign"
SCOPES = ("Ad Account Management", "Ads Management", "Creative Management", "Reporting", "Pixel Management")
MAX_PAGES = 5
PAGE_SIZE = 1000
CREATE_RETRIES = 1

CHECKLIST = [
    ("Create a TikTok Ads Manager account", "Sign up with your business email and fill in the business details. The "
     "billing country sets the currency and can't be changed.",
     "https://ads.tiktok.com/resources/help/article/create-tiktok-ads-manager-account"),
    ("Add payment", "Choose automatic or manual payment in Ads Manager's payment settings. Ads can't run without it.",
     "https://ads.tiktok.com/"),
    ("Set up the TikTok Pixel", "Needed to optimize for purchases or form sign-ups. Traffic campaigns run without it.",
     "https://ads.tiktok.com/resources/help/article/get-started-pixel"),
]

OBJECTIVES = {"sales": "WEB_CONVERSIONS", "leads": "WEB_CONVERSIONS", "traffic": "TRAFFIC", "awareness": "REACH"}
GOALS = {"traffic": ("CLICK", "CPC"), "awareness": ("REACH", "CPM")}
# Website events TikTok optimizes for (sandbox: confirm each optimization_event value).
EVENTS = {"Purchase": "SHOPPING", "Lead": "FORM", "CompleteRegistration": "ON_WEB_REGISTER", "AddToCart": "ON_WEB_CART",
          "InitiateCheckout": "INITIATE_ORDER", "ViewContent": "ON_WEB_DETAIL"}
EVENT_WORDS = {"SHOPPING": "purchases", "FORM": "form sign-ups", "ON_WEB_REGISTER": "registrations",
               "ON_WEB_CART": "adds to cart", "INITIATE_ORDER": "checkouts started", "ON_WEB_DETAIL": "product views",
               "CLICK": "clicks", "REACH": "reach"}
CTAS = {"SHOP_NOW": "SHOP_NOW", "LEARN_MORE": "LEARN_MORE", "SIGN_UP": "SIGN_UP", "BOOK_NOW": "BOOK_NOW",
        "ORDER_NOW": "ORDER_NOW", "APPLY_NOW": "APPLY_NOW", "DOWNLOAD": "DOWNLOAD_NOW", "SUBSCRIBE": "SUBSCRIBE",
        "GET_QUOTE": "GET_QUOTE", "CONTACT_US": "CONTACT_US", "GET_OFFER": "LEARN_MORE", "CALL_NOW": "CONTACT_US"}
# TikTok's age bands; a band is used when it overlaps the member's ages.
AGE_BANDS = [("AGE_13_17", 13, 17), ("AGE_18_24", 18, 24), ("AGE_25_34", 25, 34), ("AGE_35_44", 35, 44),
             ("AGE_45_54", 45, 54), ("AGE_55_100", 55, 100)]
# Countries as location_ids: GeoNames ids (sandbox: confirm). Others aren't offered here yet.
COUNTRIES = {"US": "6252001", "CA": "6251999", "GB": "2635167", "AU": "2077456", "NZ": "2186224", "IE": "2963597",
             "DE": "2921044", "FR": "3017382", "ES": "2510769", "IT": "3175395", "NL": "2750405", "BE": "2802361",
             "AT": "2782113", "CH": "2658434", "SE": "2661886", "NO": "3144096", "DK": "2623032", "FI": "660013",
             "PT": "2264397", "PL": "798544", "MX": "3996063", "BR": "3469034", "JP": "1861060", "KR": "1835841",
             "SG": "1880251", "MY": "1733045", "PH": "1694008", "AE": "290557", "SA": "102358"}
UTM = "utm_source=tiktok&utm_medium=paid&utm_campaign=__CAMPAIGN_ID__&utm_content=__CID__"
ACCOUNT_OK = ("STATUS_ENABLE",)
ACCOUNT_WORDS = {"STATUS_ENABLE": "Active", "STATUS_CONFIRM_FAIL": "Not approved by TikTok", "STATUS_PENDING_CONFIRM":
                 "In review at TikTok", "STATUS_LIMIT": "Limited by TikTok", "STATUS_DISABLE": "Disabled",
                 "STATUS_SELF_SERVICE_UNAUDITED": "Not verified yet"}


def flag_on() -> bool:
    """TIKTOK_ADS=1 switches TikTok connect and launch on (default off)."""
    return (os.environ.get("TIKTOK_ADS") or "").strip().lower() in ("1", "on", "true", "yes")


def enabled(ws, user=None) -> bool:
    import adplatforms
    return flag_on() and adplatforms.enabled(ws, user)


def app_id() -> str:
    return (os.environ.get("TIKTOK_APP_ID") or "").strip()


def app_secret() -> str:
    return (os.environ.get("TIKTOK_APP_SECRET") or "").strip()


def redirect_uri() -> str:
    import verify
    return verify.public_url() + "/connect/tiktok/callback"


def checklist():
    return list(CHECKLIST)


def ads_url(account_id=None) -> str:
    return ADS_MANAGER + (f"?aadvid={account_id}" if account_id else "")


def _q(params: dict) -> dict:
    """TikTok wants lists and objects in a GET as JSON strings."""
    return {k: (json.dumps(v, separators=(",", ":")) if isinstance(v, (list, dict)) else v)
            for k, v in params.items() if v is not None}


def _call(method: str, path: str, token: str = None, *, params=None, body=None, data=None, files=None, account=None,
          retries=None, max_wait=0.0) -> dict:
    """The `data` of a TikTok answer ({"code": 0, "message": "OK", "data": {...}}); adhttp raises on code != 0."""
    headers = {"Access-Token": token} if token else {}
    res = adhttp.call("tiktok", method, API + path, params=_q(params or {}), headers=headers, json_body=body, data=data,
                      files=files, account=account, retries=retries, secrets=[s for s in (token, app_secret()) if s],
                      max_wait=max_wait)
    return res.get("data") or {}


def _list(path: str, token: str, key: str, params: dict, account=None, max_wait=0.0) -> list:
    out = []
    for page in range(1, MAX_PAGES + 1):
        d = _call("GET", path, token, params=dict(params, page=page, page_size=PAGE_SIZE), account=account,
                  max_wait=max_wait)
        out += d.get(key) or []
        info = d.get("page_info") or {}
        if page >= int(info.get("total_page") or 1):
            break
    return out


class TikTok:
    key = "tiktok"
    name = "TikTok"

    def configured(self) -> bool:
        return bool(app_id() and app_secret()) and secretsbox.configured()

    def oauth_start(self, state: str, code_challenge: str = None) -> str:
        return AUTH_URL + "?" + urlencode({"app_id": app_id(), "state": state, "redirect_uri": redirect_uri()})

    def oauth_finish(self, code: str, code_verifier: str = None) -> dict:
        d = _call("POST", "/oauth2/access_token/", body={"app_id": app_id(), "secret": app_secret(), "auth_code": code},
                  retries=1)
        token = d.get("access_token")
        if not token:
            raise adhttp.AdHttpError("tiktok", "no access_token in the code exchange")
        shared = [str(a) for a in d.get("advertiser_ids") or []]
        me = _call("GET", "/user/info/", token)
        scopes = sorted({str(s) for s in d.get("scope") or []})
        return {"token": token, "expires_at": None, "scopes": scopes, "token_type": "ADVERTISER",
                "user_id": str(me.get("core_user_id") or ""), "user_name": str(me.get("display_name") or me.get("email") or "")[:80],
                "accounts": self.list_accounts(token, shared), "pages": [], "missing": []}

    def list_accounts(self, token: str, shared=None) -> list:
        """The advertisers this person shared with Campaignify: [{"id", "name", "currency", "timezone", "status",
        "usable"}]."""
        rows = _call("GET", "/oauth2/advertiser/get/", token, params={"app_id": app_id(), "secret": app_secret()})
        ids = [str(r.get("advertiser_id")) for r in rows.get("list") or [] if r.get("advertiser_id")]
        if shared:
            ids = [i for i in ids if i in shared] or ids
        out = []
        for i in range(0, len(ids), 100):
            info = _call("GET", "/advertiser/info/", token, params={"advertiser_ids": ids[i:i + 100], "fields": [
                "advertiser_id", "name", "currency", "timezone", "display_timezone", "status"]})
            for a in info.get("list") or []:
                st = str(a.get("status") or "")
                out.append({"id": str(a.get("advertiser_id")), "name": (a.get("name") or "TikTok ad account")[:120],
                            "currency": a.get("currency") or "", "timezone": a.get("display_timezone") or a.get("timezone") or "",
                            "status": ACCOUNT_WORDS.get(st, st.replace("STATUS_", "").title() or "Unknown status"),
                            "usable": st in ACCOUNT_OK})
        out.sort(key=lambda a: (not a["usable"], a["name"].lower()))
        return out

    def list_identities(self, token: str, advertiser_id: str) -> list:
        rows = _call("GET", "/identity/get/", token, params={"advertiser_id": advertiser_id,
                                                              "identity_type": "CUSTOMIZED_USER"}, account=advertiser_id)
        return [{"id": str(r["identity_id"]), "name": (r.get("display_name") or "Custom identity")[:60]}
                for r in rows.get("identity_list") or [] if r.get("identity_id")]

    def create_identity(self, token: str, advertiser_id: str, display_name: str) -> dict:
        d = _call("POST", "/identity/create/", token, body={"advertiser_id": advertiser_id, "display_name": display_name},
                  account=advertiser_id, retries=1)
        if not d.get("identity_id"):
            raise adhttp.AdHttpError("tiktok", "no identity_id in the identity create")
        return {"id": str(d["identity_id"]), "name": display_name}

    def list_pages(self, token: str) -> list:
        return []

    def list_pixels(self, token: str, account_id: str) -> list:
        """The advertiser's pixels by their pixel code (the id the browser pixel and the Events API use too, so
        conversions.py reads them the same way); the numeric pixel_id is looked up when a launch needs it."""
        rows = _list("/pixel/list/", token, "pixels", {"advertiser_id": account_id}, account=str(account_id))
        return [{"id": str(r["pixel_code"]), "name": (r.get("pixel_name") or f"Pixel {r['pixel_code']}")[:120]}
                for r in rows if r.get("pixel_code")]

    def revoke(self, token: str) -> bool:
        _call("POST", "/oauth2/revoke_token/", token, body={"app_id": app_id(), "secret": app_secret(),
                                                            "access_token": token}, retries=2)
        return True

    # ------------------------------------------------------------ launch
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
        raise NotImplementedError("manage for TikTok arrives after M6")

    validate = set_status = update_budget = _later


ADAPTER = TikTok()


# ================================================================ launch steps

def tag(launch_id, key: str) -> str:
    return f"[cfy:{launch_id}:{key}]"


def external_name(name: str, launch_id, key: str, limit: int = 512) -> str:
    t = tag(int(launch_id), key)
    return f"{(name or 'Campaignify')[:limit - len(t) - 1].strip()} {t}"


def money(minor: int, currency: str) -> float:
    """A budget in minor units as TikTok's amount in the currency (a number, 2 decimals at most)."""
    return round(int(minor) / adspec.minor_factor(currency), 2)


def age_groups(aud: dict) -> list:
    lo, hi = int(aud.get("age_min", 18)), int(aud.get("age_max", 65))
    hi = 100 if hi >= 65 else hi
    return [b for b, a, z in AGE_BANDS if a <= hi and z >= lo]


def band_ages(groups: list) -> tuple:
    """(youngest, oldest) the bands cover; oldest 100 means "and over"."""
    spans = [(a, z) for b, a, z in AGE_BANDS if b in groups]
    return (min(a for a, _ in spans), max(z for _, z in spans)) if spans else (18, 100)


def schedule(iso, tz_name, end=False):
    """A spec date as "YYYY-MM-DD HH:MM:SS" in UTC, from midnight (or the last second) in the advertiser's time zone
    (sandbox: confirm TikTok reads schedule times as UTC)."""
    if not iso:
        return None
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    d = date.fromisoformat(str(iso)[:10])
    t = datetime(d.year, d.month, d.day, 23, 59, 59) if end else datetime(d.year, d.month, d.day)
    return t.replace(tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def goal(spec) -> tuple:
    """(optimization_goal, billing_event, optimization_event or None)."""
    conv = spec.conversion or {}
    if spec.objective in ("sales", "leads") and conv.get("pixel_id"):
        ev = EVENTS.get(conv.get("event") or ("Purchase" if spec.objective == "sales" else "Lead"), "FORM")
        return "CONVERT", "OCPM", ev
    g, b = GOALS.get(spec.objective, ("CLICK", "CPC"))
    return g, b, None


def landing(c) -> str:
    url = c.destination_url or ""
    if c.utm:
        url += ("&" if "?" in url else "?") + c.utm
    return url


def plan_steps(spec, conn_row, assets: dict) -> list:
    """Uploads (video and cover), the campaign, the ad group, the ads. No call is made here."""
    adv, tz = str(conn_row["external_account_id"]), conn_row["timezone"]
    steps = []
    for i, c in enumerate(spec.creatives, start=1):
        a = assets.get(c.asset_id) or {}
        steps.append({"key": f"upload:{i}", "kind": "upload", "name": f"{spec.name} · ad {i} video",
                      "request": {"asset_id": c.asset_id, "media": "video", "path": a.get("path"),
                                  "poster": a.get("poster")}})
    steps.append({"key": "campaign", "kind": "campaign", "name": spec.name, "request": {
        "advertiser_id": adv, "objective_type": OBJECTIVES[spec.objective], "budget_mode": "BUDGET_MODE_INFINITE",
        "operation_status": "DISABLE"}})
    g, billing, event = goal(spec)
    ag = {"advertiser_id": adv, "campaign_id": "$campaign", "promotion_type": "WEBSITE",
          "placement_type": "PLACEMENT_TYPE_NORMAL", "placements": ["PLACEMENT_TIKTOK"],
          "location_ids": [COUNTRIES[c] for c in (spec.geo or {}).get("countries") or [] if c in COUNTRIES],
          "age_groups": age_groups(spec.audience or {}), "budget_mode": "BUDGET_MODE_DAY",
          "budget": money(spec.daily_budget_minor, spec.currency), "pacing": "PACING_MODE_SMOOTH",
          "optimization_goal": g, "billing_event": billing, "bid_type": "BID_TYPE_NO_BID",
          "schedule_type": "SCHEDULE_START_END" if spec.end_at else "SCHEDULE_FROM_NOW",
          "schedule_start_time": schedule(spec.start_at, tz), "operation_status": "ENABLE"}
    gender = (spec.audience or {}).get("genders") or ["all"]
    ag["gender"] = {"women": "GENDER_FEMALE", "men": "GENDER_MALE"}.get(gender[0], "GENDER_UNLIMITED")
    if spec.end_at:
        ag["schedule_end_time"] = schedule(spec.end_at, tz, end=True)
    if event:
        ag["pixel_code"] = str(spec.conversion["pixel_id"])       # resolved to TikTok's pixel_id when the step runs
        ag["optimization_event"] = event
    steps.append({"key": "adset", "kind": "adgroup", "name": f"{spec.name} · ad group",
                  "request": {k: v for k, v in ag.items() if v is not None}})
    for i, c in enumerate(spec.creatives, start=1):
        steps.append({"key": f"ad:{i}", "kind": "ad", "name": f"{spec.name} · ad {i} {c.name}".strip(), "request": {
            "advertiser_id": adv, "adgroup_id": "$adset", "creative": {
                "identity_type": "CUSTOMIZED_USER", "identity_id": str(conn_row["page_id"] or ""),
                "ad_format": "SINGLE_VIDEO", "video_id": f"$upload:{i}", "image_ids": [f"$upload:{i}.cover"],
                "ad_text": c.primary_text, "call_to_action": CTAS.get(c.cta, "LEARN_MORE"),
                "landing_page_url": landing(c)}}})
    return steps


def _fill(value, results: dict):
    if isinstance(value, dict):
        return {k: _fill(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, results) for v in value]
    if isinstance(value, str) and value.startswith("$"):
        key, _, fld = value[1:].partition(".")
        r = results.get(key) or {}
        out = r.get(fld or "id")
        if out in (None, ""):
            raise KeyError(f"step {key} has no {fld or 'id'} yet")
        return str(out)
    return value


def _md5(path: str) -> str:
    h = hashlib.md5()  # noqa: S324  TikTok's upload signature is the file's MD5
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _upload_image(path: str, ctx: dict, name: str) -> str:
    with open(path, "rb") as f:
        d = _call("POST", "/file/image/ad/upload/", ctx["token"], account=str(ctx["account"]), retries=CREATE_RETRIES,
                  data={"advertiser_id": str(ctx["account"]), "upload_type": "UPLOAD_BY_FILE",
                        "image_signature": _md5(path), "file_name": name[:100]},
                  files={"image_file": (os.path.basename(path), f, "image/jpeg")})
    if not d.get("image_id"):
        raise adhttp.AdHttpError("tiktok", "no image_id in the cover upload")
    return str(d["image_id"])


def _find_video(ctx: dict, name: str):
    rows = _list("/file/video/ad/search/", ctx["token"], "list", {"advertiser_id": str(ctx["account"])},
                 account=str(ctx["account"]))
    return next((r for r in rows if str(r.get("file_name") or "") == name), None)


def _upload(step: dict, ctx: dict) -> dict:
    """The video (by file, with its MD5) and its cover image (the poster). A re-run finds the video by its tagged file
    name; the cover is content-addressed on TikTok's side, so uploading it again is harmless."""
    req, adv = step["request"], str(ctx["account"])
    name = external_name(step["name"], ctx["launch_id"], step["key"], limit=100)
    found = _find_video(ctx, name) if ctx.get("lookup") else None
    if found:
        vid = str(found.get("video_id"))
    else:
        with open(req["path"], "rb") as f:
            d = _call("POST", "/file/video/ad/upload/", ctx["token"], account=adv, retries=CREATE_RETRIES,
                      data={"advertiser_id": adv, "upload_type": "UPLOAD_BY_FILE", "video_signature": _md5(req["path"]),
                            "file_name": name}, files={"video_file": (os.path.basename(req["path"]), f, "video/mp4")},
                      max_wait=float(ctx.get("max_wait") or 0))
        first = d[0] if isinstance(d, list) and d else ((d.get("list") or [d])[0] if isinstance(d, dict) else {})
        vid = str((first or {}).get("video_id") or "")
        if not vid:
            raise adhttp.AdHttpError("tiktok", "no video_id in the video upload")
    cover = _upload_image(req["poster"], ctx, name) if req.get("poster") and os.path.exists(req["poster"]) else None
    if not cover:
        raise adhttp.AdHttpError("tiktok", "no cover image for the video",
                                 user_message="The video has no cover picture. Make the video again.")
    return {"id": vid, "cover": cover, "object": "video", **({"found": True} if found else {})}


def _video_ready(video_id: str, ctx: dict):
    d = _call("GET", "/file/video/ad/info/", ctx["token"], account=str(ctx["account"]),
              params={"advertiser_id": str(ctx["account"]), "video_ids": [video_id]})
    v = (d.get("list") or [{}])[0]
    if v.get("displayable") is False:      # sandbox: confirm the field that says the video finished processing
        raise NotReady(15.0)


EDGES = {"campaign": ("/campaign/get/", "/campaign/create/", "campaign_id", "campaign_name"),
         "adgroup": ("/adgroup/get/", "/adgroup/create/", "adgroup_id", "adgroup_name"),
         "ad": ("/ad/get/", "/ad/create/", "ad_id", "ad_name")}


def find_tagged(kind: str, ctx: dict, key: str, parent: str = None):
    """The object of this launch step already on TikTok (a create whose answer was lost), or None. Deleted ones don't
    count."""
    t = tag(ctx["launch_id"], key)
    lst, _, idf, namef = EDGES[kind]
    params = {"advertiser_id": str(ctx["account"])}
    if kind == "adgroup" and parent:
        params["filtering"] = {"campaign_ids": [parent]}
    if kind == "ad" and parent:
        params["filtering"] = {"adgroup_ids": [parent]}
    for r in _list(lst, ctx["token"], "list", params, account=str(ctx["account"]), max_wait=float(ctx.get("max_wait") or 0)):
        if str(r.get(namef) or "").endswith(t) and "DELETE" not in str(r.get("operation_status") or r.get("secondary_status") or ""):
            return r
    return None


def _pixel_id(ctx: dict, code: str) -> str:
    rows = _list("/pixel/list/", ctx["token"], "pixels", {"advertiser_id": str(ctx["account"]), "code": code},
                 account=str(ctx["account"]))
    hit = next((r for r in rows if str(r.get("pixel_code")) == code), None)
    if not hit or not hit.get("pixel_id"):
        raise adhttp.AdHttpError("tiktok", f"pixel {code} not found", user_message="That pixel isn't on the TikTok ad "
                                 "account any more. Pick another in Settings > Ad accounts.")
    return str(hit["pixel_id"])


def execute_step(step: dict, ctx: dict) -> dict:
    """Run one step. ctx: token, account (advertiser id), launch_id, results, lookup. Returns {"id", ...}; raises adhttp
    errors or NotReady."""
    kind, key, results = step["kind"], step["key"], ctx["results"]
    if kind == "upload":
        return _upload(step, ctx)
    req = _fill(json.loads(json.dumps(step["request"])), results)
    _, create, idf, namef = EDGES[kind]
    if kind == "ad":
        _video_ready(req["creative"]["video_id"], ctx)
    if ctx.get("lookup"):
        found = find_tagged(kind, ctx, key, req.get("campaign_id") or req.get("adgroup_id"))
        if found:
            return {"id": str(found.get(idf)), "found": True}
    name = external_name(step["name"], ctx["launch_id"], key)
    if kind == "ad":
        body = {"advertiser_id": req["advertiser_id"], "adgroup_id": req["adgroup_id"],
                "creatives": [dict(req["creative"], ad_name=name)]}
    else:
        body = dict(req, **{namef: name})
        if body.get("pixel_code"):
            body["pixel_id"] = _pixel_id(ctx, body.pop("pixel_code"))
    d = _call("POST", create, ctx["token"], body=body, account=str(ctx["account"]), retries=CREATE_RETRIES,
              max_wait=float(ctx.get("max_wait") or 0))
    oid = (d.get("ad_ids") or [None])[0] if kind == "ad" else d.get(idf)
    if not oid:
        raise adhttp.AdHttpError("tiktok", f"no {idf} in the {kind} create")
    return {"id": str(oid), "status": "PAUSED" if kind == "campaign" else "ENABLED"}


STATUS_PATHS = {"campaign": ("/campaign/status/update/", "campaign_ids"),
                "adgroup": ("/adgroup/status/update/", "adgroup_ids"), "ad": ("/ad/status/update/", "ad_ids")}


def _status(kind: str, oid: str, status: str, ctx: dict):
    path, ids = STATUS_PATHS[kind]
    _call("POST", path, ctx["token"], body={"advertiser_id": str(ctx["account"]), ids: [oid], "operation_status": status},
          account=str(ctx["account"]), max_wait=float(ctx.get("max_wait") or 0))


def _read(kind: str, oid: str, ctx: dict):
    lst, _, idf, _ = EDGES[kind]
    flt = {"campaign": "campaign_ids", "adgroup": "adgroup_ids", "ad": "ad_ids"}[kind]
    d = _call("GET", lst, ctx["token"], account=str(ctx["account"]), max_wait=float(ctx.get("max_wait") or 0),
              params={"advertiser_id": str(ctx["account"]), "filtering": {flt: [oid]}})
    return next((r for r in d.get("list") or [] if str(r.get(idf)) == str(oid)), None)


def compensate(step: dict, ctx: dict) -> str:
    """Undo one done step of a failed launch: ads are deleted ("compensated"); the campaign is turned off if someone
    turned it on, and kept with its ad group ("kept"); uploads stay in the library."""
    kind, ext = step["kind"], step.get("external_id")
    if not ext:
        return "compensated"
    if kind == "ad":
        _status("ad", ext, "DELETE", ctx)
        return "compensated"
    if kind == "campaign":
        cur = _read("campaign", ext, ctx)
        if cur and str(cur.get("operation_status") or "DISABLE") != "DISABLE":
            _status("campaign", ext, "DISABLE", ctx)
    return "kept"


def verify(step: dict, ctx: dict) -> bool:
    kind, ext = step["kind"], step.get("external_id")
    if not ext:
        return False
    if kind == "upload":
        return True               # a video id stays valid in the library; the cover is uploaded again if needed
    obj = _read(kind, ext, ctx)
    return bool(obj) and "DELETE" not in str(obj.get("operation_status") or obj.get("secondary_status") or "")


# ================================================================ reporting

def _minor(value, currency: str) -> int:
    try:
        return int((Decimal(str(value or "0")) * adspec.minor_factor(currency)).quantize(Decimal("1"), ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def pull_insights(ctx: dict, objects, day_range) -> list:
    """Daily numbers for the launched campaigns, in the shape of ad_insights_daily. ctx: token, account, currency.
    day_range: (first, last) in the advertiser's own dates."""
    ids = [str(o["external_id"]) for o in objects if o.get("kind") == "campaign" and o.get("external_id")]
    if not ids:
        return []
    first, last = (str(d)[:10] for d in day_range)
    cur = ctx.get("currency") or "USD"
    rows = _list("/report/integrated/get/", ctx["token"], "list", {
        "advertiser_id": str(ctx["account"]), "report_type": "BASIC", "data_level": "AUCTION_CAMPAIGN",
        "dimensions": ["campaign_id", "stat_time_day"], "start_date": first, "end_date": last,
        "metrics": ["spend", "impressions", "clicks", "conversion", "complete_payment", "total_complete_payment_rate"],
        "filtering": [{"field_name": "campaign_ids", "filter_type": "IN", "filter_value": json.dumps(ids)}]},
        account=str(ctx["account"]))
    out = []
    for r in rows:
        dm, m = r.get("dimensions") or {}, r.get("metrics") or {}

        def n(k):
            try:
                return int(float(m.get(k) or 0))
            except (TypeError, ValueError):
                return 0
        out.append({"platform": "tiktok", "external_object_id": str(dm.get("campaign_id") or ""), "level": "campaign",
                    "day": str(dm.get("stat_time_day") or "")[:10], "spend_minor": _minor(m.get("spend"), cur),
                    "currency": cur, "impressions": n("impressions"), "clicks": n("clicks"), "link_clicks": n("clicks"),
                    "leads": 0, "purchases": n("complete_payment"), "conversions": n("conversion"),
                    "raw_json": json.dumps(m, separators=(",", ":"))[:2000]})
    return out


# ================================================================ errors in plain words

RECONNECT = "TikTok ended Campaignify's access to this ad account. Reconnect it in Settings > Ad accounts, then try again."
NETWORK_WORDS = "We couldn't reach TikTok. Try again in a few minutes."
WAITED_WORDS = "TikTok kept asking us to slow down for over 2 hours. Try again later."
PERMISSION = ("Campaignify doesn't have this permission on the TikTok ad account. Reconnect it and allow every permission "
              "TikTok asks about, or ask the account's admin to give you Operator access.")


def translate(err) -> str:
    if isinstance(err, adhttp.NetworkError):
        return NETWORK_WORDS
    if isinstance(err, adhttp.TokenInvalid):
        return RECONNECT
    if isinstance(err, adhttp.PermissionDenied) or err.code == 40001:
        return PERMISSION
    if err.user_message:
        return f"TikTok said: {err.user_message[:300]}"
    return "TikTok refused part of the campaign. The details are below."


def retryable(err) -> bool:
    return isinstance(err, adhttp.AdHttpError) and ((err.status or 0) >= 500 or err.code in adhttp.TIKTOK_TRANSIENT) \
        and not isinstance(err, (adhttp.TokenInvalid, adhttp.PermissionDenied))

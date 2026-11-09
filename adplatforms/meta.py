"""Meta (Facebook and Instagram) adapter. M2.1: connect only, read only.

Facebook Login for Business with a login configuration (META_LOGIN_CONFIG_ID picks the
permissions; we pass no `scope`), the authorization code flow with PKCE, then:

    1. code -> token          GET /oauth/access_token (client_secret, redirect_uri, code_verifier)
    2. check the token        GET /debug_token with the app token: it must be for our app and valid;
                              gives the token type, expiry, granted scopes and the Meta user id
    3. long-lived token       a USER token is exchanged (fb_exchange_token, about 60 days); a
                              SYSTEM_USER token (Business Integration) does not expire and is kept as is
    4. what can be picked     GET /me/adaccounts, /me/accounts (Pages) and each Page's Instagram
                              account; after the pick, GET /act_<id>/adspixels

Connecting creates or changes nothing on an ad account: every call is a GET, except DELETE
/me/permissions when a connection is disconnected (it removes our app's access, nothing on the
account). Launching (M2.3, the second half of this file) creates PAUSED objects only, after the
member approved the card that lists them: ad images and videos, ad creatives (object_story_spec),
one campaign, one ad set (promoted_object) and the ads.

Every call goes through adhttp.call (timeouts, retries, the per-account point budget, scrubbed
errors). The token goes in the Authorization header, never the URL, with appsecret_proof.

Settings (GitHub secrets / variables, passed by deploy.yml):
    META_APP_ID            the Rector & Stull app id
    META_APP_SECRET        the app secret (a secret)
    META_LOGIN_CONFIG_ID   the Facebook Login for Business configuration id
    TOKEN_KEY              the token box key (secretsbox.py)
The redirect URI registered in the app must be exactly RS_PUBLIC_URL + /connect/meta/callback,
i.e. https://campaignify.ai/connect/meta/callback in production.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode

import adhttp
import secretsbox

# The Graph / Marketing API version every call uses. One place: bump it here.
# v26.0 shipped 2026-07-29 and is supported until at least mid 2028 (developers.facebook.com
# /docs/graph-api/changelog/versions, read 2026-11-08). Meta reportedly released v27.0 on
# 2026-11-04; move to it after a tester connect passes on it.
GRAPH_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
DIALOG = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"

# The permissions the login configuration must grant (roadmap M0.1). M2.1 reads with ads_read,
# pages_show_list and business_management; the rest are for creatives and launches (M2.3). A token
# missing any of them still connects, and the page says which to grant on Reconnect.
SCOPES = ("ads_read", "ads_management", "pages_show_list", "pages_read_engagement", "pages_manage_ads",
          "business_management")

MAX_PAGES = 5            # Graph pages of results we follow per listing (100 items each)
MAX_IG_LOOKUPS = 25      # Pages we ask for a linked Instagram account

# account_status (Marketing API AdAccount): what we show and whether it can be picked
ACCOUNT_STATUS = {1: ("Active", True), 2: ("Disabled on Meta", False), 3: ("Unpaid balance on Meta", True),
                  7: ("In review on Meta", True), 8: ("Pending settlement", True), 9: ("In grace period", True),
                  100: ("Closing", False), 101: ("Closed", False), 201: ("Active", True), 202: ("Closed", False)}

# "No ad account yet?" checklist: adhub.py's Meta steps 1 to 4, each with a deep link into Meta.
CHECKLIST_LINKS = ["https://business.facebook.com/",
                   "https://business.facebook.com/settings/ad-accounts",
                   "https://business.facebook.com/billing_hub/payment_settings",
                   "https://www.facebook.com/pages/create"]


def app_id() -> str:
    return (os.environ.get("META_APP_ID") or "").strip()


def app_secret() -> str:
    return (os.environ.get("META_APP_SECRET") or "").strip()


def config_id() -> str:
    return (os.environ.get("META_LOGIN_CONFIG_ID") or "").strip()


def redirect_uri() -> str:
    import verify
    return verify.public_url() + "/connect/meta/callback"


class BadSignedRequest(ValueError):
    """A signed_request that isn't Meta's (bad format, wrong algorithm, bad signature, no user id)."""


def _b64url(part: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def parse_signed_request(signed_request: str) -> dict:
    """The payload of a Meta signed_request (deauthorize and data deletion callbacks), verified the way Meta
    specifies: "<base64url signature>.<base64url JSON payload>", the signature being HMAC-SHA256 of the encoded
    payload with the app secret, and the payload's algorithm HMAC-SHA256. Raises BadSignedRequest."""
    secret = app_secret()
    if not secret:
        raise BadSignedRequest("the app secret isn't set")
    sr = (signed_request or "").strip()
    if sr.count(".") != 1 or len(sr) > 8192:
        raise BadSignedRequest("not a signed request")
    sig_part, payload_part = sr.split(".", 1)
    try:
        sig = _b64url(sig_part)
        payload = json.loads(_b64url(payload_part))
    except (ValueError, TypeError):
        raise BadSignedRequest("not a signed request") from None
    want = hmac.new(secret.encode(), payload_part.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, want):
        raise BadSignedRequest("bad signature")
    if not isinstance(payload, dict) or str(payload.get("algorithm") or "").upper() != "HMAC-SHA256":
        raise BadSignedRequest("wrong algorithm")
    if not str(payload.get("user_id") or "").strip():
        raise BadSignedRequest("no user id")
    return payload


def checklist():
    """[(title, text, link)] for people who have no ad account yet (no API account creation, D1)."""
    import adhub
    steps = adhub.PLATFORMS["meta"]["steps"][:4]
    return [(t, d, CHECKLIST_LINKS[i] if i < len(CHECKLIST_LINKS) else link) for i, (t, d, link) in enumerate(steps)]


def _proof(token: str) -> str:
    return hmac.new(app_secret().encode(), token.encode(), hashlib.sha256).hexdigest()


def _get(path: str, token: str = None, params=None, account: str = None, app_token: bool = False, max_wait: float = 0.0):
    params = dict(params or {})
    headers = {}
    secrets = [app_secret()]
    if token:
        headers["Authorization"] = f"Bearer {token}"
        params["appsecret_proof"] = _proof(token)
        secrets.append(token)
    if app_token:
        params["access_token"] = f"{app_id()}|{app_secret()}"
    return adhttp.call("meta", "GET", GRAPH + path, params=params, headers=headers, account=account, secrets=secrets,
                       max_wait=max_wait)


def _paged(path: str, token: str, params: dict, account: str = None) -> list:
    out, after = [], None
    for _ in range(MAX_PAGES):
        q = dict(params)
        if after:
            q["after"] = after
        body = _get(path, token, q, account=account)
        out.extend(body.get("data") or [])
        after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (body.get("paging") or {}).get("next"):
            break
    return out


def _iso(epoch) -> str:
    try:
        epoch = int(epoch or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat() if epoch > 0 else None


class Meta:
    key = "meta"
    name = "Meta (Facebook and Instagram)"

    def configured(self) -> bool:
        return bool(app_id() and app_secret() and config_id()) and secretsbox.configured()

    def oauth_start(self, state: str, code_challenge: str) -> str:
        q = {"client_id": app_id(), "redirect_uri": redirect_uri(), "state": state, "config_id": config_id(),
             "response_type": "code", "override_default_response_type": "true",
             "code_challenge": code_challenge, "code_challenge_method": "S256"}
        return DIALOG + "?" + urlencode(q)

    def exchange_code(self, code: str, code_verifier: str) -> str:
        body = adhttp.call("meta", "GET", GRAPH + "/oauth/access_token", retries=1,
                           params={"client_id": app_id(), "client_secret": app_secret(), "redirect_uri": redirect_uri(),
                                   "code": code, "code_verifier": code_verifier})
        tok = body.get("access_token")
        if not tok:
            raise adhttp.AdHttpError("meta", "no access_token in the code exchange")
        return tok

    def debug(self, token: str) -> dict:
        """Meta's own check of the token: for our app, valid. Its type, expiry, scopes and user id."""
        data = _get("/debug_token", params={"input_token": token}, app_token=True).get("data") or {}
        if str(data.get("app_id") or "") != app_id():
            raise adhttp.TokenInvalid("meta", "token is for another app")
        if not data.get("is_valid"):
            raise adhttp.TokenInvalid("meta", "token is not valid")
        return data

    def long_lived(self, token: str) -> tuple:
        """(token, expires_at epoch or 0) for a 60-day user token."""
        body = adhttp.call("meta", "GET", GRAPH + "/oauth/access_token", retries=2, secrets=[token],
                           params={"grant_type": "fb_exchange_token", "client_id": app_id(),
                                   "client_secret": app_secret(), "fb_exchange_token": token})
        new = body.get("access_token")
        if not new:
            raise adhttp.AdHttpError("meta", "no access_token in the long-lived exchange")
        expires_in = int(body.get("expires_in") or 0)
        return new, (int(adhttp.CLOCK()) + expires_in) if expires_in else 0

    def oauth_finish(self, code: str, code_verifier: str) -> dict:
        """Everything a pending connection needs: the token, its expiry and scopes, and what can be picked."""
        token = self.exchange_code(code, code_verifier)
        info = self.debug(token)
        expires = int(info.get("expires_at") or 0)
        if (info.get("type") or "USER").upper() == "USER" and expires:
            try:
                token, new_exp = self.long_lived(token)
                expires = new_exp or int(self.debug(token).get("expires_at") or 0)
            except adhttp.AdHttpError:
                pass          # keep the short-lived token; the page shows when it expires
        me = _get("/me", token, {"fields": "id,name"})
        scopes = sorted(set(info.get("scopes") or []))
        return {"token": token, "expires_at": _iso(expires), "scopes": scopes, "token_type": info.get("type") or "USER",
                "user_id": str(me.get("id") or info.get("user_id") or ""), "user_name": (me.get("name") or "")[:80],
                "accounts": self.list_accounts(token), "pages": self.list_pages(token),
                "missing": [s for s in SCOPES if s not in scopes]}

    def list_accounts(self, token: str) -> list:
        fields = "account_id,name,currency,timezone_name,account_status"
        try:
            rows = _paged("/me/adaccounts", token, {"fields": fields + ",business{id,name}", "limit": 100})
        except adhttp.PermissionDenied:      # no business_management: the same list without the portfolio
            rows = _paged("/me/adaccounts", token, {"fields": fields, "limit": 100})
        out = []
        for r in rows:
            aid = str(r.get("account_id") or str(r.get("id") or "").replace("act_", ""))
            if not aid.isdigit():
                continue
            label, usable = ACCOUNT_STATUS.get(int(r.get("account_status") or 0), ("Unknown status", True))
            biz = r.get("business") or {}
            out.append({"id": aid, "name": (r.get("name") or f"Ad account {aid}")[:120], "currency": r.get("currency") or "",
                        "timezone": r.get("timezone_name") or "", "status": label, "usable": usable,
                        "business_id": str(biz.get("id") or "") or None, "business_name": (biz.get("name") or "")[:120]})
        return out

    def list_pages(self, token: str) -> list:
        pages = []
        for r in _paged("/me/accounts", token, {"fields": "id,name", "limit": 100}):
            pid = str(r.get("id") or "")
            if pid.isdigit():
                pages.append({"id": pid, "name": (r.get("name") or f"Page {pid}")[:120], "ig_id": None, "ig_username": None})
        for p in pages[:MAX_IG_LOOKUPS]:
            # The Page's linked Instagram professional account (instagram_user_id on ad creatives).
            # A Page without one, or a permission we lack, just means Facebook-only ads.
            try:
                ig = _get(f"/{p['id']}", token, {"fields": "instagram_business_account{id,username}"}) \
                    .get("instagram_business_account") or {}
            except adhttp.TokenInvalid:
                raise
            except adhttp.AdHttpError:
                ig = {}
            if str(ig.get("id") or "").isdigit():
                p["ig_id"], p["ig_username"] = str(ig["id"]), (ig.get("username") or "")[:60]
        return pages

    def list_pixels(self, token: str, account_id: str) -> list:
        rows = _paged(f"/act_{account_id}/adspixels", token, {"fields": "id,name", "limit": 50}, account=f"act_{account_id}")
        return [{"id": str(r["id"]), "name": (r.get("name") or f"Pixel {r['id']}")[:120]}
                for r in rows if str(r.get("id") or "").isdigit()]

    def revoke(self, token: str) -> bool:
        """Remove our app's access for the person who connected (DELETE /me/permissions)."""
        body = adhttp.call("meta", "DELETE", GRAPH + "/me/permissions", retries=2, secrets=[token, app_secret()],
                           headers={"Authorization": f"Bearer {token}"}, params={"appsecret_proof": _proof(token)})
        return bool(body.get("success"))

    # ------------------------------------------------------------ launch (M2.3): everything is created PAUSED
    # launches.py runs these as a saga: plan_steps once at approval (the ledger in launch_steps), then
    # execute_step for each step in order, compensate in reverse when one fails. Each object's name ends in
    # tag(launch_id, step_key), "[cfy:<launch id>:<step>]", so a step that may already have run (a timeout, a
    # crash, a retry) finds its object on Meta instead of making a second one.

    def plan_steps(self, spec, conn_row, assets: dict) -> list:
        """The exact calls for a CampaignSpec, in Meta's order: uploads, creatives, the campaign, the ad set,
        the ads. assets: {asset_id: {"kind", "path", "poster"}} from the library. Each step is
        {"key", "kind", "name", "request"}; "$<key>" / "$<key>.<field>" in a request is filled at run time from
        an earlier step's result. No call is made here."""
        return plan_steps(spec, conn_row, assets)

    def execute_step(self, step: dict, ctx: dict) -> dict:
        return execute_step(step, ctx)

    def compensate(self, step: dict, ctx: dict) -> str:
        return compensate(step, ctx)

    def verify(self, step: dict, ctx: dict) -> bool:
        return verify(step, ctx)

    def upload_asset(self, ctx: dict, step: dict) -> dict:
        return _upload(step, ctx)

    # Manage and report (M2.4, M2.5).
    def _later(self, *a, **kw):
        raise NotImplementedError("arrives in M2.4 / M2.5")

    validate = set_status = update_budget = pull_insights = _later


ADAPTER = Meta()


# ================================================================ launch steps (M2.3)
# Marketing API objects and fields as documented for v26.0 (developers.facebook.com/docs/marketing-api, read
# 2026-11-08). Hand-checked against Meta's reference, not yet against a live account: the sandbox run
# (tools/meta_sandbox_launch.py) re-records tests/fixtures/meta/launch_*.json.

OBJECTIVES = {"sales": "OUTCOME_SALES", "leads": "OUTCOME_LEADS", "traffic": "OUTCOME_TRAFFIC",
              "awareness": "OUTCOME_AWARENESS"}
OPTIMIZATION = {"sales": "OFFSITE_CONVERSIONS", "leads": "OFFSITE_CONVERSIONS", "traffic": "LINK_CLICKS",
                "awareness": "REACH"}
EVENT_TYPES = {"Purchase": "PURCHASE", "Lead": "LEAD", "CompleteRegistration": "COMPLETE_REGISTRATION",
               "AddToCart": "ADD_TO_CART", "InitiateCheckout": "INITIATED_CHECKOUT", "Contact": "CONTACT",
               "Schedule": "SCHEDULE", "SubmitApplication": "SUBMIT_APPLICATION", "ViewContent": "CONTENT_VIEW"}
# Meta's special_ad_categories values. "credit" became FINANCIAL_PRODUCTS_SERVICES in 2025 (CREDIT is deprecated).
SPECIAL_CATEGORIES = {"housing": "HOUSING", "credit": "FINANCIAL_PRODUCTS_SERVICES", "employment": "EMPLOYMENT",
                      "social_issues": "ISSUES_ELECTIONS_POLITICS"}
GENDERS = {"men": 1, "male": 1, "women": 2, "female": 2}
PLACEMENTS = {   # spec placement -> (publisher platform, positions field, position)
    "feed": [("facebook", "facebook_positions", "feed"), ("instagram", "instagram_positions", "stream")],
    "stories": [("facebook", "facebook_positions", "story"), ("instagram", "instagram_positions", "story")],
    "reels": [("facebook", "facebook_positions", "facebook_reels"), ("instagram", "instagram_positions", "reels")],
    "right_column": [("facebook", "facebook_positions", "right_hand_column")],
    "explore": [("instagram", "instagram_positions", "explore")],
    "messenger": [("messenger", "messenger_positions", "messenger_home")],
    "audience_network": [("audience_network", "audience_network_positions", "classic")],
}
# Meta has no Marketing API field we could confirm (as of 2026-11-08) for the "AI-generated or edited" box that
# Ads Manager shows on an ad. When the sandbox run confirms one, set it here, e.g. ("ad", "<field>", <value>), and
# every ad of a launch with ai_disclosure gets it. Until then the approval card and the launch page tell the
# member to tick it in Ads Manager before turning the campaign on (the campaign is PAUSED, so nothing runs first).
AI_DISCLOSURE_FIELD = None
VIDEO_READY_WAIT_S = 20 * 60       # a video still processing after this fails the launch
CREATE_RETRIES = 1                 # adhttp attempts for a create; the saga looks up, then tries again


class NotReady(Exception):
    """A video Meta is still processing: try the step again in `retry_after` seconds."""

    def __init__(self, retry_after=15.0):
        super().__init__("still processing")
        self.retry_after = retry_after


def tag(launch_id, key: str) -> str:
    """The idempotency tag every object's name ends with."""
    return f"[cfy:{int(launch_id)}:{key}]"


def external_name(name: str, launch_id, key: str) -> str:
    return f"{(name or 'Campaignify')[:180].strip()} {tag(launch_id, key)}"


def _act(account_id) -> str:
    a = str(account_id or "")
    return a if a.startswith("act_") else f"act_{a}"


def _auth(token: str):
    return {"Authorization": f"Bearer {token}"}, {"appsecret_proof": _proof(token)}, [token, app_secret()]


def _post(path: str, token: str, data: dict, account: str, files=None, edit_of: str = None, creates: bool = True,
          max_wait: float = 0.0):
    headers, params, secrets = _auth(token)
    body = {k: (v if isinstance(v, str) else json.dumps(v, separators=(",", ":")))
            for k, v in data.items() if v is not None}
    return adhttp.call("meta", "POST", GRAPH + path, params=params, data=body, headers=headers, files=files,
                       account=account, retries=CREATE_RETRIES if creates else None, secrets=secrets, edit_of=edit_of,
                       max_wait=max_wait)


def _delete(object_id: str, token: str, account: str, max_wait: float = 0.0):
    headers, params, secrets = _auth(token)
    return adhttp.call("meta", "DELETE", f"{GRAPH}/{object_id}", params=params, headers=headers, account=account,
                       secrets=secrets, edit_of=object_id, max_wait=max_wait)


def _money(minor: int, currency: str) -> str:
    return f"{minor / 100:,.2f} {currency}"


def _when(iso: str | None, tz_name: str | None):
    """An ISO date or datetime as Meta's start_time / end_time, at midnight in the ad account's time zone."""
    if not iso:
        return None
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:  # noqa: BLE001  an unknown zone name: UTC
        tz = timezone.utc
    d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.isoformat()


def targeting(spec) -> dict:
    """The ad set's targeting from the spec's geo and audience (interests are left to Meta in v1)."""
    geo, aud = spec.geo or {}, spec.audience or {}
    t = {"geo_locations": {}, "age_min": int(aud.get("age_min", 18)), "age_max": int(aud.get("age_max", 65)),
         # Keep the member's ages and genders as limits, not suggestions (Advantage+ audience off).
         "targeting_automation": {"advantage_audience": 0}}
    if geo.get("countries"):
        t["geo_locations"]["countries"] = list(geo["countries"])
    if geo.get("zips"):
        t["geo_locations"]["zips"] = [{"key": f"{(geo.get('countries') or ['US'])[0]}:{z}"} for z in geo["zips"]]
    genders = sorted({GENDERS[g] for g in (aud.get("genders") or []) if g in GENDERS})
    if genders and len(genders) == 1:
        t["genders"] = genders
    if isinstance(spec.placements, list) and spec.placements:
        pubs, pos = [], {}
        for p in spec.placements:
            for pub, fld, val in PLACEMENTS.get(p, []):
                if pub not in pubs:
                    pubs.append(pub)
                pos.setdefault(fld, [])
                if val not in pos[fld]:
                    pos[fld].append(val)
        if pubs:
            t["publisher_platforms"] = pubs
            t.update(pos)
    return t


def plan_steps(spec, conn_row, assets: dict) -> list:
    page_id = conn_row["page_id"]
    ig_id = conn_row["instagram_actor_id"]
    steps = []
    for i, c in enumerate(spec.creatives, start=1):
        a = assets.get(c.asset_id) or {}
        steps.append({"key": f"upload:{i}", "kind": "upload", "name": f"{spec.name} · ad {i} {c.kind}",
                      "request": {"asset_id": c.asset_id, "media": c.kind, "path": a.get("path"),
                                  "poster": a.get("poster")}})
    for i, c in enumerate(spec.creatives, start=1):
        link = c.destination_url
        cta = {"type": c.cta, "value": {"link": link}}
        if c.kind == "video":
            story = {"page_id": page_id, "video_data": {
                "video_id": f"$upload:{i}", "image_hash": f"$upload:{i}.thumb_hash", "message": c.primary_text,
                "title": c.headline, "link_description": c.description, "call_to_action": cta}}
        else:
            story = {"page_id": page_id, "link_data": {
                "image_hash": f"$upload:{i}.hash", "link": link, "message": c.primary_text, "name": c.headline,
                "description": c.description, "call_to_action": cta}}
        if ig_id:
            story["instagram_user_id"] = ig_id
        _drop_empty(story)
        req = {"object_story_spec": story}
        if c.utm:
            req["url_tags"] = c.utm
        steps.append({"key": f"creative:{i}", "kind": "creative", "name": f"{spec.name} · ad {i} {c.name}".strip(),
                      "request": req})
    camp = {"objective": OBJECTIVES[spec.objective], "status": "PAUSED", "buying_type": "AUCTION",
            "special_ad_categories": [SPECIAL_CATEGORIES[s] for s in spec.special_ad_categories or []
                                      if s in SPECIAL_CATEGORIES],
            # One daily budget for the campaign (Advantage campaign budget), in the account's minor units.
            "daily_budget": str(int(spec.daily_budget_minor)), "bid_strategy": "LOWEST_COST_WITHOUT_CAP"}
    steps.append({"key": "campaign", "kind": "campaign", "name": spec.name, "request": camp})
    adset = {"campaign_id": "$campaign", "status": "PAUSED", "billing_event": "IMPRESSIONS",
             "optimization_goal": OPTIMIZATION[spec.objective], "targeting": targeting(spec),
             "start_time": _when(spec.start_at, conn_row["timezone"]), "end_time": _when(spec.end_at, conn_row["timezone"])}
    if spec.objective in ("sales", "leads") and (spec.conversion or {}).get("pixel_id"):
        adset["promoted_object"] = {"pixel_id": str(spec.conversion["pixel_id"]),
                                    "custom_event_type": EVENT_TYPES.get(spec.conversion.get("event") or
                                                                         ("Purchase" if spec.objective == "sales" else "Lead"))}
    if spec.objective in ("sales", "leads", "traffic"):
        adset["destination_type"] = "WEBSITE"
    steps.append({"key": "adset", "kind": "adset", "name": f"{spec.name} · ad set", "request": adset})
    for i, c in enumerate(spec.creatives, start=1):
        ad = {"adset_id": "$adset", "creative": {"creative_id": f"$creative:{i}"}, "status": "PAUSED"}
        if spec.ai_disclosure and AI_DISCLOSURE_FIELD:
            ad[AI_DISCLOSURE_FIELD[1]] = AI_DISCLOSURE_FIELD[2]
        steps.append({"key": f"ad:{i}", "kind": "ad", "name": f"{spec.name} · ad {i} {c.name}".strip(), "request": ad})
    for s in steps:
        s["request"] = {k: v for k, v in s["request"].items() if v is not None}
    return steps


def _drop_empty(d: dict):
    for k in list(d):
        if isinstance(d[k], dict):
            _drop_empty(d[k])
        elif d[k] in (None, ""):
            del d[k]


def _fill(value, results: dict):
    """Replace "$key" / "$key.field" with an earlier step's result."""
    if isinstance(value, dict):
        return {k: _fill(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, results) for v in value]
    if isinstance(value, str) and value.startswith("$"):
        key, _, fld = value[1:].partition(".")
        r = results.get(key)
        if not r:
            raise KeyError(f"step {key} has no result yet")
        out = r.get(fld or "id")
        if out in (None, ""):
            raise KeyError(f"step {key} has no {fld or 'id'}")
        return str(out)
    return value


EDGES = {"creative": "adcreatives", "campaign": "campaigns", "adset": "adsets", "ad": "ads"}


def find_tagged(kind: str, ctx: dict, key: str):
    """The object of this launch step already on Meta (a create that timed out, a crashed run), or None.
    Campaigns, ad sets and ads are filtered by name on Meta's side; creatives and videos are listed and
    matched here. Deleted objects don't come back (Meta leaves them out by default)."""
    t = tag(ctx["launch_id"], key)
    token, account = ctx["token"], _act(ctx["account"])
    if kind in ("campaign", "adset", "ad"):
        rows = _paged(f"/{account}/{EDGES[kind]}", token, {
            "fields": "id,name,effective_status", "limit": 50,
            "filtering": json.dumps([{"field": "name", "operator": "CONTAIN", "value": t}])},
            account=account)
    elif kind == "creative":
        rows = _paged(f"/{account}/adcreatives", token, {"fields": "id,name", "limit": 100}, account=account)
    elif kind == "video":
        rows = [dict(r, name=r.get("title") or r.get("name") or "")
                for r in _paged(f"/{account}/advideos", token, {"fields": "id,title", "limit": 100}, account=account)]
    else:
        return None
    for r in rows:
        if str(r.get("name") or "").endswith(t) and str(r.get("effective_status") or "") not in ("DELETED", "ARCHIVED"):
            return r
    return None


def _upload(step: dict, ctx: dict) -> dict:
    req, token, account = step["request"], ctx["token"], _act(ctx["account"])
    name = external_name(step["name"], ctx["launch_id"], step["key"])
    if req["media"] == "video":
        found = find_tagged("video", ctx, step["key"]) if ctx.get("lookup") else None
        if found:
            vid = str(found["id"])
        else:
            with open(req["path"], "rb") as f:
                body = _post(f"/{account}/advideos", token, {"name": name, "title": name}, account,
                             files={"source": (os.path.basename(req["path"]), f, "video/mp4")})
            vid = str(body.get("id") or "")
            if not vid:
                raise adhttp.AdHttpError("meta", "no id in the video upload")
        thumb = None
        if req.get("poster") and os.path.exists(req["poster"]):
            thumb = _image_hash(req["poster"], token, account)
        return {"id": vid, "thumb_hash": thumb, "object": "video"}
    h = _image_hash(req["path"], token, account)
    return {"id": h, "hash": h, "object": "image"}


def _image_hash(path: str, token: str, account: str) -> str:
    """Upload an image (adimages, base64 bytes). Meta keys images by their content, so uploading the same
    file again gives the same hash and no new object: no lookup is needed."""
    import base64
    with open(path, "rb") as f:
        raw = f.read()
    body = _post(f"/{account}/adimages", token, {"bytes": base64.b64encode(raw).decode()}, account)
    images = body.get("images") or {}
    first = next(iter(images.values()), {}) if isinstance(images, dict) else {}
    h = str(first.get("hash") or "")
    if not h:
        raise adhttp.AdHttpError("meta", "no hash in the image upload")
    return h


def _video_ready(video_id: str, ctx: dict):
    body = _get(f"/{video_id}", ctx["token"], {"fields": "status"}, account=_act(ctx["account"]))
    st = str(((body.get("status") or {}).get("video_status")) or "").lower()
    if st in ("ready", ""):
        return
    if st == "error":
        raise adhttp.AdHttpError("meta", f"video {video_id} failed processing on Meta",
                                 user_message="Meta couldn't process the video. Try another export of it.")
    raise NotReady(15.0)


def execute_step(step: dict, ctx: dict) -> dict:
    """Run one step. Returns its result ({"id", ...}). Looks for the step's object first when the step may have
    run before (ctx["lookup"]), so a re-run makes nothing new. Raises adhttp errors or NotReady."""
    kind, key, results = step["kind"], step["key"], ctx["results"]
    token, account = ctx["token"], _act(ctx["account"])
    if kind == "upload":
        return _upload(step, ctx)
    if kind == "creative":
        story = step["request"].get("object_story_spec") or {}
        if "video_data" in story:
            _video_ready(_fill(story["video_data"]["video_id"], results), ctx)
    if ctx.get("lookup"):
        found = find_tagged(kind, ctx, key)
        if found:
            return {"id": str(found["id"]), "found": True}
    data = _fill(dict(step["request"]), results)
    data["name"] = external_name(step["name"], ctx["launch_id"], key)
    body = _post(f"/{account}/{EDGES[kind]}", token, data, account)
    oid = str(body.get("id") or "")
    if not oid:
        raise adhttp.AdHttpError("meta", f"no id in the {kind} create")
    return {"id": oid}


def compensate(step: dict, ctx: dict) -> str:
    """Undo one done step of a failed launch: "compensated" (deleted on Meta) for creatives and ads;
    "kept" for the campaign and ad set (left PAUSED, since deleting a campaign is final) and for uploads (Meta
    keeps images by content; a video stays in the account's library). Raises when Meta refuses."""
    kind, ext = step["kind"], step.get("external_id")
    token, account, wait = ctx["token"], _act(ctx["account"]), float(ctx.get("max_wait") or 0)
    if not ext:
        return "compensated"
    if kind in ("creative", "ad"):
        _delete(ext, token, account, max_wait=wait)
        return "compensated"
    if kind in ("campaign", "adset"):
        cur = _get(f"/{ext}", token, {"fields": "status,effective_status"}, account=account, max_wait=wait)
        if str(cur.get("status") or "PAUSED") != "PAUSED":
            _post(f"/{ext}", token, {"status": "PAUSED"}, account, edit_of=ext, creates=False, max_wait=wait)
        return "kept"
    return "kept"


def verify(step: dict, ctx: dict) -> bool:
    """A kept object of an earlier run still exists (before a retry reuses it)."""
    kind, ext = step["kind"], step.get("external_id")
    if not ext:
        return False
    if kind == "upload" and (step.get("result") or {}).get("object") == "image":
        return True                          # content-addressed: uploading again is the same image
    try:
        body = _get(f"/{ext}", ctx["token"], {"fields": "id,effective_status" if kind in ("campaign", "adset", "ad")
                                              else "id"}, account=_act(ctx["account"]))
    except adhttp.TokenInvalid:
        raise
    except adhttp.AdHttpError as e:
        if e.code == 100:                    # unsupported get request: it's gone
            return False
        raise
    return str(body.get("effective_status") or "") not in ("DELETED", "ARCHIVED")

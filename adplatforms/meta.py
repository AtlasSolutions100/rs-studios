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

Nothing here creates or changes anything on an ad account: every call is a GET, except
DELETE /me/permissions when a connection is disconnected (it removes our app's access, nothing on
the account). Campaign creation is M2.3.

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
    import json
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


def _get(path: str, token: str = None, params=None, account: str = None, app_token: bool = False):
    params = dict(params or {})
    headers = {}
    secrets = [app_secret()]
    if token:
        headers["Authorization"] = f"Bearer {token}"
        params["appsecret_proof"] = _proof(token)
        secrets.append(token)
    if app_token:
        params["access_token"] = f"{app_id()}|{app_secret()}"
    return adhttp.call("meta", "GET", GRAPH + path, params=params, headers=headers, account=account, secrets=secrets)


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

    # Launch, manage and report (M2.3 to M2.5).
    def _later(self, *a, **kw):
        raise NotImplementedError("campaign launch arrives in M2.3")

    validate = plan_steps = execute_step = compensate = set_status = update_budget = pull_insights = upload_asset = _later


ADAPTER = Meta()

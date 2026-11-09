"""Google Ads adapter (roadmap M3.1 to M3.3). The REST interface of the Google Ads API with `requests`
through adhttp.call (no client library; the house style of google_auth.py and meta.py).

Connect (M3.1), read only:
    1. OAuth 2.0 web flow with PKCE: scope openid, email and https://www.googleapis.com/auth/adwords,
       access_type=offline and prompt=consent, so Google always returns a refresh token
    2. code -> tokens          POST oauth2.googleapis.com/token; the refresh token goes straight into the
                               token box (secretsbox.py) and is the only credential kept. Access tokens (1 hour)
                               are minted from it per run and live in this process only
    3. who connected           the id_token's `sub` and email (it came over TLS from Google's token endpoint,
                               so it isn't re-verified; it's only used to tell people apart)
    4. what can be picked      GET customers:listAccessibleCustomers, then GAQL on each: the customer itself,
                               and for a manager its whole hierarchy (customer_client), so a client reached
                               through a manager is listed with the manager as its login-customer-id
Disconnect revokes the refresh token at Google (POST oauth2.googleapis.com/revoke).

Launch (M3.2 Search, M3.3 Performance Max; the next PRs): everything in one GoogleAdsService.Mutate with temporary ids
(negative resource ids), so Google makes all of it or none of it. The campaign is PAUSED; its ad group,
keywords and ad (or asset group) are ENABLED under it, so turning the one campaign on is all it takes and
nothing serves before that. Every name ends in "[cfy:<launch id>:<step>]": a run that may have happened
already (a lost answer, a crash) finds its campaign with GAQL instead of making a second one, and Google
refuses a second campaign with the same name anyway (DUPLICATE_CAMPAIGN_NAME). Status comes from GAQL.

Access (research B2, Google's developer-token page as of 2026-11-08): developer tokens were sunset on
2026-09-09. The API ignores the developer-token header, and the access level (Test, Explorer, Basic,
Standard) belongs to the Google Cloud project that owns the OAuth client below. So no developer token is
sent. Test access reaches test accounts only (CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION otherwise).

Settings (GitHub secrets / variables, passed by deploy.yml):
    GOOGLE_ADS                  the flag: 1 turns Google connect and launch on (default off), on top of AD_LAUNCH
    GOOGLE_ADS_CLIENT_ID        optional: the OAuth client id; unset = GOOGLE_OAUTH_CLIENT_ID (the sign-in client)
    GOOGLE_ADS_CLIENT_SECRET    optional: its secret; unset = GOOGLE_OAUTH_CLIENT_SECRET
    TOKEN_KEY                   the token box key (secretsbox.py)
The OAuth client must list RS_PUBLIC_URL + /connect/google/callback as an authorized redirect URI, i.e.
https://campaignify.ai/connect/google/callback in production.
"""
import base64
import hashlib
import json
import os
import re
import threading
from urllib.parse import urlencode

import adhttp
import secretsbox

# The Google Ads API version every call uses. One place: bump it here. v25 shipped 2026-07-22 and sunsets
# in Aug 2027 (developers.google.com/google-ads/api/docs/sunset-dates, read 2026-11-08); v26 is out. Move
# to it after tools/google_sandbox_launch.py passes on it.
API_VERSION = "v25"
API = f"https://googleads.googleapis.com/{API_VERSION}"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE_ADWORDS = "https://www.googleapis.com/auth/adwords"
SCOPES = ("openid", "email", SCOPE_ADWORDS)

MAX_ROOTS = 20            # accessible customers we read
MAX_ACCOUNTS = 200        # customers listed in the picker
MAX_PAGES = 5             # GAQL result pages we follow (10,000 rows each)
CREATE_RETRIES = 1        # adhttp attempts for a mutate; the saga looks up, then tries again
CUSTOMER_STATUS = {"ENABLED": ("Active", True), "CANCELED": ("Cancelled on Google Ads", False),
                   "SUSPENDED": ("Suspended on Google Ads", False), "CLOSED": ("Closed", False),
                   "UNKNOWN": ("Unknown status", False), "UNSPECIFIED": ("Unknown status", False)}

# "No Google Ads account yet?" (decision D1: the member makes it in the Google Ads UI, billing included).
CHECKLIST = [
    ("Create a Google Ads account", "Sign in at Google Ads with the Google account you want to use. When it asks "
     "for a first campaign, choose to switch to Expert mode and create the account without a campaign.",
     "https://ads.google.com/home/"),
    ("Pick the country, time zone and currency", "They can't be changed later. Use the currency you want "
     "budgets in.", "https://support.google.com/google-ads/answer/6366720"),
    ("Set up billing", "In Google Ads, open Billing, then Settings, and add the payment method. Campaigns can't "
     "run without it.", "https://ads.google.com/aw/billing/settings"),
    ("Note the 10-digit customer ID", "It's at the top right in Google Ads, like 123-456-7890. You'll pick it here.",
     "https://support.google.com/google-ads/answer/1704344"),
]


def flag_on() -> bool:
    """GOOGLE_ADS=1 switches Google connect and launch on (default off)."""
    return (os.environ.get("GOOGLE_ADS") or "").strip().lower() in ("1", "on", "true", "yes")


def enabled(ws, user=None) -> bool:
    """Google shows only with both flags: AD_LAUNCH for this workspace (adplatforms.enabled) and GOOGLE_ADS."""
    import adplatforms
    return flag_on() and adplatforms.enabled(ws, user)


def client_id() -> str:
    import google_auth
    return (os.environ.get("GOOGLE_ADS_CLIENT_ID") or "").strip() or google_auth.client_id()


def client_secret() -> str:
    import google_auth
    return (os.environ.get("GOOGLE_ADS_CLIENT_SECRET") or "").strip() or google_auth.client_secret()


def redirect_uri() -> str:
    import verify
    return verify.public_url() + "/connect/google/callback"


def checklist():
    return list(CHECKLIST)


def dashed(cid) -> str:
    """1234567890 -> 123-456-7890 (how Google Ads shows a customer id)."""
    d = re.sub(r"\D", "", str(cid or ""))
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else d


def ads_url(customer_id=None) -> str:
    """Google Ads, campaigns view. The account switcher there picks the customer."""
    return "https://ads.google.com/aw/campaigns"


def _jwt_claims(id_token: str) -> dict:
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}


# ---------------------------------------------------------------- access tokens (memory only)

_ACCESS = {}                 # sha256(refresh token) -> (access token, expires epoch)
_LOCK = threading.Lock()


def access_token(refresh_token: str) -> str:
    """A fresh access token for a stored refresh token (cached in this process until a minute before it ends).
    An expired or revoked grant raises adhttp.TokenInvalid (invalid_grant): the connection needs Reconnect."""
    k = hashlib.sha256(refresh_token.encode()).hexdigest()
    now = adhttp.CLOCK()
    with _LOCK:
        hit = _ACCESS.get(k)
        if hit and hit[1] - 60 > now:
            return hit[0]
    body = adhttp.call("google", "POST", TOKEN_URL, retries=2, secrets=[refresh_token, client_secret()],
                       data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                             "client_id": client_id(), "client_secret": client_secret()})
    tok = body.get("access_token")
    if not tok:
        raise adhttp.AdHttpError("google", "no access_token in the refresh answer")
    with _LOCK:
        _ACCESS[k] = (tok, now + int(body.get("expires_in") or 3600))
    return tok


def forget_access(refresh_token: str):
    with _LOCK:
        _ACCESS.pop(hashlib.sha256(refresh_token.encode()).hexdigest(), None)


def _headers(access: str, login=None) -> dict:
    h = {"Authorization": f"Bearer {access}"}
    if login:
        h["login-customer-id"] = re.sub(r"\D", "", str(login))
    return h


def _call(method: str, path: str, access: str, *, login=None, body=None, account=None, retries=None, max_wait=0.0):
    return adhttp.call("google", method, API + path, headers=_headers(access, login), json_body=body,
                       account=account, retries=retries, secrets=[access], max_wait=max_wait)


def search(access: str, customer: str, query: str, login=None, max_wait: float = 0.0) -> list:
    """GAQL: every row of `query` on `customer` (GoogleAdsService.Search, following nextPageToken)."""
    rows, token = [], None
    for _ in range(MAX_PAGES):
        body = {"query": query}
        if token:
            body["pageToken"] = token
        res = _call("POST", f"/customers/{customer}/googleAds:search", access, login=login, body=body,
                    account=str(customer), max_wait=max_wait)
        rows.extend(res.get("results") or [])
        token = res.get("nextPageToken")
        if not token:
            break
    return rows


def gaql_str(value: str) -> str:
    """A GAQL string literal. Names we make never hold quotes or backslashes (clean_name), so this is a guard."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


class Google:
    key = "google"
    name = "Google Ads"

    def configured(self) -> bool:
        return bool(client_id() and client_secret()) and secretsbox.configured()

    def oauth_start(self, state: str, code_challenge: str, login_hint: str = None) -> str:
        q = {"client_id": client_id(), "redirect_uri": redirect_uri(), "response_type": "code",
             "scope": " ".join(SCOPES), "state": state, "access_type": "offline", "prompt": "consent",
             "include_granted_scopes": "false", "code_challenge": code_challenge, "code_challenge_method": "S256"}
        return AUTH_URL + "?" + urlencode(q)

    def oauth_finish(self, code: str, code_verifier: str) -> dict:
        """Everything a pending connection needs: the refresh token (for the box), the scopes, who connected
        and the customers that can be picked. Raises adhttp errors (scrubbed), or AdHttpError with a
        user_message when the adwords permission was left unticked or no refresh token came back."""
        body = adhttp.call("google", "POST", TOKEN_URL, retries=1, secrets=[code, code_verifier, client_secret()],
                           data={"code": code, "code_verifier": code_verifier, "client_id": client_id(),
                                 "client_secret": client_secret(), "redirect_uri": redirect_uri(),
                                 "grant_type": "authorization_code"})
        access, refresh = body.get("access_token"), body.get("refresh_token")
        scopes = sorted(set(str(body.get("scope") or "").split()))
        if not access:
            raise adhttp.AdHttpError("google", "no access_token in the code exchange")
        if SCOPE_ADWORDS not in scopes:
            raise adhttp.PermissionDenied("google", "the adwords scope was not granted", code="oauth.scope_missing",
                                          user_message="Google didn't give Campaignify access to Google Ads. Connect "
                                                       "again and tick the Google Ads box on Google's screen.")
        if not refresh:
            raise adhttp.AdHttpError("google", "no refresh_token in the code exchange", code="oauth.no_refresh",
                                     user_message="Google didn't send a lasting permission. Connect again.")
        who = _jwt_claims(body.get("id_token") or "")
        with _LOCK:
            _ACCESS[hashlib.sha256(refresh.encode()).hexdigest()] = (access, adhttp.CLOCK() + int(body.get("expires_in") or 3600))
        return {"token": refresh, "expires_at": None, "scopes": scopes, "token_type": "REFRESH",
                "user_id": str(who.get("sub") or ""), "user_name": str(who.get("email") or "")[:80],
                "accounts": self.list_accounts(access), "pages": [], "missing": []}

    def list_accounts(self, access: str) -> list:
        """The customers this Google login can use: [{"id", "name", "currency", "timezone", "status", "usable",
        "manager", "test", "login", "via", "level"}]. Managers are listed (not usable: campaigns go on a client);
        a client is reached directly when possible, else through its top manager (login = that manager's id)."""
        res = _call("GET", "/customers:listAccessibleCustomers", access)
        roots = [r.split("/")[-1] for r in res.get("resourceNames") or [] if re.fullmatch(r"customers/\d{10}", str(r))]
        out, seen = [], {}
        fields = "id, descriptive_name, currency_code, time_zone, manager, test_account, status"

        def add(row: dict, login, via, level):
            cid = str(row.get("id") or "")
            if not cid.isdigit() or (cid in seen and (seen[cid]["login"] is None or login is not None)):
                return
            label, ok = CUSTOMER_STATUS.get(str(row.get("status") or "UNKNOWN"), ("Unknown status", False))
            item = {"id": cid, "name": (row.get("descriptiveName") or f"Google Ads {dashed(cid)}")[:120],
                    "currency": row.get("currencyCode") or "", "timezone": row.get("timeZone") or "",
                    "status": label, "manager": bool(row.get("manager")), "test": bool(row.get("testAccount")),
                    "usable": ok and not row.get("manager"), "login": login, "via": via, "level": level}
            if cid in seen:
                out[out.index(seen[cid])] = item
            else:
                out.append(item)
            seen[cid] = item

        for root in roots[:MAX_ROOTS]:
            try:
                rows = search(access, root, f"SELECT customer.{fields.replace(', ', ', customer.')} FROM customer LIMIT 1")
            except adhttp.TokenInvalid:
                raise
            except adhttp.AdHttpError as e:
                # a cancelled or unfinished account answers CUSTOMER_NOT_ENABLED: list it, not pickable
                add({"id": root, "status": "CANCELED" if e.code == "authorizationError.CUSTOMER_NOT_ENABLED"
                     else "UNKNOWN"}, None, None, 0)
                continue
            me = (rows[0] if rows else {}).get("customer") or {"id": root}
            add(me, None, None, 0)
            if not me.get("manager") or len(out) >= MAX_ACCOUNTS:
                continue
            try:
                kids = search(access, root, "SELECT customer_client.id, customer_client.descriptive_name, "
                              "customer_client.currency_code, customer_client.time_zone, customer_client.manager, "
                              "customer_client.test_account, customer_client.status, customer_client.level "
                              "FROM customer_client WHERE customer_client.level > 0", login=root)
            except adhttp.TokenInvalid:
                raise
            except adhttp.AdHttpError:
                continue
            for k in kids:
                cc = k.get("customerClient") or {}
                add(cc, root, me.get("descriptiveName") or dashed(root), int(cc.get("level") or 1))
                if len(out) >= MAX_ACCOUNTS:
                    break
        out.sort(key=lambda a: (not a["usable"], a["manager"], a["name"].lower()))
        return out

    def list_pages(self, token: str) -> list:
        return []

    def list_pixels(self, token: str, account_id: str) -> list:
        return []           # conversion actions arrive with M3.5

    def revoke(self, token: str) -> bool:
        """Revoke the refresh token at Google: Campaignify's access to Google Ads (and the sign-in scopes of this
        grant) ends for this Google login."""
        forget_access(token)
        body = adhttp.call("google", "POST", REVOKE_URL, retries=2, secrets=[token], data={"token": token})
        return isinstance(body, dict) and not body.get("error")

    # Launch, manage and report: the launch arrives with M3.2 (Search) and M3.3 (Performance Max).
    def _later(self, *a, **kw):
        raise NotImplementedError("arrives in M3.2 / M3.3")

    plan_steps = execute_step = compensate = verify = upload_asset = _later
    validate = set_status = update_budget = pull_insights = _later


ADAPTER = Google()


# ---------------------------------------------------------------- errors in plain words

ERRORS = {
    "authenticationError.OAUTH_TOKEN_EXPIRED": "Google ended Campaignify's access to this account. Reconnect it in "
                                               "Settings > Ad accounts, then try again.",
    "authenticationError.OAUTH_TOKEN_REVOKED": "Google ended Campaignify's access to this account. Reconnect it in "
                                               "Settings > Ad accounts, then try again.",
    "oauth.invalid_grant": "Google ended Campaignify's access to this account. Reconnect it in Settings > Ad accounts, "
                           "then try again.",
    "authenticationError.NOT_ADS_USER": "That Google login has no Google Ads account. Create one in Google Ads, or "
                                        "connect with the login that has one.",
    "authorizationError.USER_PERMISSION_DENIED": "This Google login can't make campaigns in that Google Ads account. "
                                                 "Ask its admin for Standard or Admin access, then reconnect.",
    "authorizationError.CUSTOMER_NOT_ENABLED": "That Google Ads account isn't active: it was cancelled, or its setup "
                                               "isn't finished. Finish it in Google Ads, then try again.",
    "authorizationError.CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION": "Campaignify's Google Ads access is still approved "
                                                                    "for test accounts only. Email us and we'll tell "
                                                                    "you when it opens up.",
    "authorizationError.DEVELOPER_TOKEN_NOT_APPROVED": "Campaignify's Google Ads access is still approved for test "
                                                       "accounts only. Email us and we'll tell you when it opens up.",
    "authorizationError.ACTION_NOT_PERMITTED": "Google doesn't allow this action for Campaignify's access level yet. "
                                               "Email us and we'll sort it out.",
    "headerError.INVALID_LOGIN_CUSTOMER_ID": "The manager account this Google Ads account was reached through changed. "
                                             "Reconnect it in Settings > Ad accounts.",
    "quotaError.RESOURCE_EXHAUSTED": "Google asked us to slow down for a long time. Try again later.",
    "policyFindingError.POLICY_FINDING": "Google's ad policies flagged some of the ad text. Change the flagged "
                                         "headline or description and try again.",
    "policyViolationError.POLICY_ERROR": "Google's ad policies flagged a keyword. Remove or change it and try again.",
    "campaignBudgetError.MONEY_AMOUNT_TOO_SMALL": "The daily budget is under what Google takes for this currency. "
                                                  "Raise it a little.",
    "campaignError.REQUIRED_BUSINESS_NAME_ASSET_NOT_LINKED": "Performance Max needs the business name. Add it and try "
                                                             "again.",
    "campaignError.REQUIRED_LOGO_ASSET_NOT_LINKED": "Performance Max needs at least 1 logo. Add one and try again.",
    "assetGroupError.NOT_ENOUGH_HEADLINE_ASSET": "Performance Max needs at least 3 headlines.",
    "assetGroupError.NOT_ENOUGH_LONG_HEADLINE_ASSET": "Performance Max needs at least 1 long headline.",
    "assetGroupError.NOT_ENOUGH_DESCRIPTION_ASSET": "Performance Max needs at least 2 descriptions.",
    "assetGroupError.NOT_ENOUGH_MARKETING_IMAGE_ASSET": "Performance Max needs at least 1 landscape image.",
    "assetGroupError.NOT_ENOUGH_SQUARE_MARKETING_IMAGE_ASSET": "Performance Max needs at least 1 square image.",
    "imageError.ASPECT_RATIO_NOT_ALLOWED": "Google refused an image's shape. Use another image.",
    "notAllowlistedError.CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE": "Google hasn't opened this feature to this Google "
                                                                     "Ads account yet.",
}
NETWORK_WORDS = "We couldn't reach Google. Try again in a few minutes."
WAITED_WORDS = "Google kept asking us to slow down for over 2 hours. Try again later."
RECONNECT = ERRORS["authenticationError.OAUTH_TOKEN_EXPIRED"]


def translate(err) -> str:
    if isinstance(err, adhttp.NetworkError):
        return NETWORK_WORDS
    msg = ERRORS.get(err.code or "")
    if msg:
        if err.code in ("policyFindingError.POLICY_FINDING", "policyViolationError.POLICY_ERROR") and err.user_message:
            msg += f" Google said: {err.user_message[:200]}"
        return msg
    if isinstance(err, adhttp.TokenInvalid):
        return RECONNECT
    if isinstance(err, adhttp.PermissionDenied):
        return ERRORS["authorizationError.USER_PERMISSION_DENIED"]
    if err.user_message:
        return f"Google said: {err.user_message[:300]}"
    return "Google refused part of the campaign. The details are below."


def retryable(err) -> bool:
    return isinstance(err, adhttp.AdHttpError) and (err.code in adhttp.GOOGLE_TRANSIENT or (err.status or 0) >= 500) \
        and not isinstance(err, (adhttp.TokenInvalid, adhttp.PermissionDenied))

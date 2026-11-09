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

Launch (M3.2 Search, M3.3 Performance Max): everything in one GoogleAdsService.Mutate with temporary ids
(negative resource ids), so Google makes all of it or none of it. The campaign is PAUSED; its ad group,
keywords and ad (or asset group) are ENABLED under it, so turning the one campaign on is all it takes and
nothing serves before that. Every name ends in "[cfy:<launch id>:<step>]": a run that may have happened
already (a lost answer, a crash) finds its campaign with GAQL instead of making a second one, and Google
refuses a second campaign with the same name anyway (DUPLICATE_CAMPAIGN_NAME). Status comes from GAQL.

Report: pull_insights reads daily campaign numbers with one GAQL search (campaign by segments.date: cost,
impressions, clicks, conversions and their value) for adinsights.py, which stores them in ad_insights_daily.

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
import adspec
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
SCOPE_DATAMANAGER = "https://www.googleapis.com/auth/datamanager"   # leads to Google (conversions.py, M3.4)

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


def scopes() -> tuple:
    """What connect asks for: SCOPES, plus the Data Manager API's scope while CONVERSIONS_API is on (leads from
    Campaignify pages go to Google through it, conversions.py). Optional on Google's screen: without it the
    connection still launches, and Settings > Conversion tracking says to reconnect for server-side leads."""
    import conversions
    return SCOPES + ((SCOPE_DATAMANAGER,) if conversions.ws_flag_on() else ())


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
             "scope": " ".join(scopes()), "state": state, "access_type": "offline", "prompt": "consent",
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
        return []           # Google has conversion actions instead: conversion_actions.py (M3.5)

    def revoke(self, token: str) -> bool:
        """Revoke the refresh token at Google: Campaignify's access to Google Ads (and the sign-in scopes of this
        grant) ends for this Google login."""
        forget_access(token)
        body = adhttp.call("google", "POST", REVOKE_URL, retries=2, secrets=[token], data={"token": token})
        return isinstance(body, dict) and not body.get("error")

    # ------------------------------------------------------------ launch (M3.2, M3.3)

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
        raise NotImplementedError("manage for Google arrives after M3.3")

    validate = set_status = update_budget = _later


ADAPTER = Google()


# ================================================================ launch: the one mutate
# REST field names (lowerCamelCase) as in the v25 reference (developers.google.com/google-ads/api/rest, read
# 2026-11-08), checked against Google's Search and Performance Max guides, not yet against a live account:
# tools/google_sandbox_launch.py records the real answers into tests/fixtures/google/recorded/.

# Countries: Google's country criterion id is 2000 + the ISO 3166-1 numeric code (US 840 -> 2840).
COUNTRIES = {"US": 840, "CA": 124, "GB": 826, "AU": 36, "NZ": 554, "IE": 372, "DE": 276, "FR": 250, "ES": 724,
             "IT": 380, "NL": 528, "BE": 56, "AT": 40, "CH": 756, "SE": 752, "NO": 578, "DK": 208, "FI": 246,
             "PT": 620, "PL": 616, "MX": 484, "BR": 76, "AR": 32, "CL": 152, "CO": 170, "PE": 604, "IN": 356,
             "JP": 392, "KR": 410, "SG": 702, "HK": 344, "MY": 458, "PH": 608, "ZA": 710, "AE": 784, "SA": 682,
             "IL": 376, "TR": 792, "CZ": 203, "GR": 300, "RO": 642, "HU": 348}
LANGUAGES = {"en": 1000, "de": 1001, "fr": 1002, "es": 1003, "it": 1004, "ja": 1005, "nl": 1010, "pt": 1014}
LANGUAGE_WORDS = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "ja": "Japanese",
                  "nl": "Dutch", "pt": "Portuguese"}
MATCH = {"exact": "EXACT", "phrase": "PHRASE", "broad": "BROAD"}
HEADLINE_PIN = {1: "HEADLINE_1", 2: "HEADLINE_2", 3: "HEADLINE_3"}
DESCRIPTION_PIN = {1: "DESCRIPTION_1", 2: "DESCRIPTION_2"}
NO_EU_POLITICAL = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
URL_SUFFIX = "utm_source=google&utm_medium=cpc&utm_campaign={campaignid}&utm_content={creative}"
# Performance Max: what Campaignify approved is what runs, so Google doesn't write extra text from the site.
PMAX_OPT_OUT = ("TEXT_ASSET_AUTOMATION", "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION")
# Image sizes we upload (Google's recommended sizes; minimums 600x314, 300x300, 480x600, logo 128x128).
PMAX_CROPS = {"MARKETING_IMAGE": ("pmax-wide", 1200, 628), "SQUARE_MARKETING_IMAGE": ("pmax-square", 1200, 1200),
              "PORTRAIT_MARKETING_IMAGE": ("pmax-portrait", 960, 1200)}
LOGO_MIN = 128
FIELD_WORDS = {"MARKETING_IMAGE": "landscape image", "SQUARE_MARKETING_IMAGE": "square image",
               "PORTRAIT_MARKETING_IMAGE": "portrait image", "LOGO": "logo", "LANDSCAPE_LOGO": "landscape logo"}


def tag(launch_id, key: str) -> str:
    return f"[cfy:{launch_id}:{key}]"


def clean_name(name: str, limit: int = 180) -> str:
    """A name safe in GAQL and in our "{lid}" fill: no quotes, backslashes or braces."""
    return re.sub(r"\s+", " ", re.sub(r"['\"\\{}]", "", str(name or "Campaignify"))).strip()[:limit] or "Campaignify"


def _named(name: str, key: str) -> str:
    """The external name; "{lid}" becomes the launch id at run time (the ledger is planned before the id exists)."""
    return f"{clean_name(name)} {tag('{lid}', key)}"


def micros(minor: int, currency: str) -> str:
    """A budget in minor units as Google's amount_micros (int64, sent as a string)."""
    return str(int(minor) * (1_000_000 // adspec.minor_factor(currency)))


def _when(iso, end=False):
    """A spec date as Google's start_date_time / end_date_time ("yyyy-MM-dd HH:mm:ss", in the account's zone)."""
    if not iso:
        return None
    d = str(iso)[:10]
    return f"{d} 23:59:59" if end else f"{d} 00:00:00"


def _rn(customer: str, kind: str, n: int) -> str:
    return f"customers/{customer}/{kind}/{n}"


def geo_ops(customer: str, camp: str, spec) -> list:
    ops = []
    for c in (spec.geo or {}).get("countries") or []:
        if c in COUNTRIES:
            ops.append({"campaignCriterionOperation": {"create": {
                "campaign": camp, "location": {"geoTargetConstant": f"geoTargetConstants/{2000 + COUNTRIES[c]}"}}}})
    lang = LANGUAGES.get(((spec.google or {}).get("language") or "en"))
    if lang:
        ops.append({"campaignCriterionOperation": {"create": {
            "campaign": camp, "language": {"languageConstant": f"languageConstants/{lang}"}}}})
    return ops


def _budget_op(customer, spec, key="campaign"):
    return {"campaignBudgetOperation": {"create": {
        "resourceName": _rn(customer, "campaignBudgets", -1), "name": _named(f"{spec.name} budget", key),
        "amountMicros": micros(spec.daily_budget_minor, spec.currency), "deliveryMethod": "STANDARD",
        "explicitlyShared": False}}}


def bidding(spec) -> tuple:
    """(field, words). Search: Maximize clicks unless the member asked for conversions; PMax: conversions."""
    g = spec.google or {}
    if g.get("type") == "pmax" or g.get("bidding") == "conversions":
        return "maximizeConversions", "Maximize conversions"
    return "targetSpend", "Maximize clicks"


def search_operations(spec, customer: str) -> list:
    """Budget, campaign (PAUSED), locations and language, negative keywords, ad group, keywords, the RSA."""
    g = spec.google or {}
    budget, camp, group = _rn(customer, "campaignBudgets", -1), _rn(customer, "campaigns", -2), _rn(customer, "adGroups", -3)
    bid_field, _ = bidding(spec)
    c = {"resourceName": camp, "name": _named(spec.name, "campaign"), "status": "PAUSED",
         "advertisingChannelType": "SEARCH", "campaignBudget": budget, bid_field: {},
         "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": bool(g.get("search_partners", True)),
                             "targetContentNetwork": False, "targetPartnerSearchNetwork": False},
         "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
         "containsEuPoliticalAdvertising": NO_EU_POLITICAL, "finalUrlSuffix": URL_SUFFIX}
    if spec.start_at:
        c["startDateTime"] = _when(spec.start_at)
    if spec.end_at:
        c["endDateTime"] = _when(spec.end_at, end=True)
    ops = [_budget_op(customer, spec), {"campaignOperation": {"create": c}}]
    ops += geo_ops(customer, camp, spec)
    for k in g.get("negative_keywords") or []:
        term = _term(k)
        if term:
            ops.append({"campaignCriterionOperation": {"create": {
                "campaign": camp, "negative": True,
                "keyword": {"text": term, "matchType": MATCH.get(_match(k), "PHRASE")}}}})
    ops.append({"adGroupOperation": {"create": {"resourceName": group, "name": _named(f"{spec.name} ad group", "campaign"),
                                                "campaign": camp, "status": "ENABLED", "type": "SEARCH_STANDARD"}}})
    for k in g.get("keywords") or []:
        term = _term(k)
        if term:
            ops.append({"adGroupCriterionOperation": {"create": {
                "adGroup": group, "status": "ENABLED", "keyword": {"text": term, "matchType": MATCH.get(_match(k), "PHRASE")}}}})
    hp, dp = g.get("headline_pins") or [], g.get("description_pins") or []
    heads = []
    for i, h in enumerate(g.get("headlines") or []):
        a = {"text": h}
        pin = hp[i] if i < len(hp) else 0
        if pin in HEADLINE_PIN:
            a["pinnedField"] = HEADLINE_PIN[pin]
        heads.append(a)
    descs = []
    for i, d in enumerate(g.get("descriptions") or []):
        a = {"text": d}
        pin = dp[i] if i < len(dp) else 0
        if pin in DESCRIPTION_PIN:
            a["pinnedField"] = DESCRIPTION_PIN[pin]
        descs.append(a)
    rsa = {"headlines": heads, "descriptions": descs}
    for p in ("path1", "path2"):
        if g.get(p):
            rsa[p] = g[p]
    ops.append({"adGroupAdOperation": {"create": {"adGroup": group, "status": "ENABLED",
                                                  "ad": {"finalUrls": [g.get("final_url") or ""],
                                                         "responsiveSearchAd": rsa}}}})
    return ops


def _term(k) -> str:
    return str((k.get("term") or k.get("text") or k.get("keyword")) if isinstance(k, dict) else k or "").strip()


def _match(k) -> str:
    return str((k.get("match") or "phrase") if isinstance(k, dict) else "phrase").lower()


def pmax_operations(spec, customer: str, uploads: list) -> list:
    """Budget, campaign (PAUSED, brand guidelines on), business name and logos at campaign level, locations and
    language, one asset group with its text assets and images. uploads: [(step key, field type)]."""
    g = spec.google or {}
    budget, camp, group = _rn(customer, "campaignBudgets", -1), _rn(customer, "campaigns", -2), _rn(customer, "assetGroups", -3)
    c = {"resourceName": camp, "name": _named(spec.name, "campaign"), "status": "PAUSED",
         "advertisingChannelType": "PERFORMANCE_MAX", "campaignBudget": budget, "maximizeConversions": {},
         "brandGuidelinesEnabled": True, "containsEuPoliticalAdvertising": NO_EU_POLITICAL, "finalUrlSuffix": URL_SUFFIX,
         "assetAutomationSettings": [{"assetAutomationType": t, "assetAutomationStatus": "OPTED_OUT"} for t in PMAX_OPT_OUT]}
    if spec.start_at:
        c["startDateTime"] = _when(spec.start_at)
    if spec.end_at:
        c["endDateTime"] = _when(spec.end_at, end=True)
    ops = [_budget_op(customer, spec), {"campaignOperation": {"create": c}}]
    nxt = [-10]

    def text_asset(t: str) -> str:
        rn = _rn(customer, "assets", nxt[0])
        nxt[0] -= 1
        ops.append({"assetOperation": {"create": {"resourceName": rn, "textAsset": {"text": t}}}})
        return rn
    biz = text_asset(g.get("business_name") or "")
    ops.append({"campaignAssetOperation": {"create": {"campaign": camp, "asset": biz, "fieldType": "BUSINESS_NAME"}}})
    for key, field in uploads:
        if field in ("LOGO", "LANDSCAPE_LOGO"):
            ops.append({"campaignAssetOperation": {"create": {"campaign": camp, "asset": f"${key}", "fieldType": field}}})
    ops += geo_ops(customer, camp, spec)
    url = (spec.creatives[0].destination_url if spec.creatives else "") or g.get("final_url") or ""
    ops.append({"assetGroupOperation": {"create": {"resourceName": group, "name": _named(f"{spec.name} asset group", "campaign"),
                                                   "campaign": camp, "finalUrls": [url], "status": "ENABLED"}}})
    links = []
    for fld, key in (("HEADLINE", "headlines"), ("LONG_HEADLINE", "long_headlines"), ("DESCRIPTION", "descriptions")):
        for t in g.get(key) or []:
            links.append((text_asset(t), fld))
    for key, field in uploads:
        if field not in ("LOGO", "LANDSCAPE_LOGO"):
            links.append((f"${key}", field))
    for rn, fld in links:
        ops.append({"assetGroupAssetOperation": {"create": {"assetGroup": group, "asset": rn, "fieldType": fld}}})
    return ops


def plan_uploads(spec, assets: dict) -> list:
    """[(key, field, request)] for a PMax launch: each picked image as a landscape and a square crop (and a
    portrait one when it is 4:5), then each logo. assets: {asset id: {"path", ...}} and {"logo:<photo id>": {...}}."""
    out, n = [], 0
    for c in spec.creatives:
        a = assets.get(c.asset_id) or {}
        fields = ["MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE"] + (["PORTRAIT_MARKETING_IMAGE"] if c.aspect == "4:5" else [])
        for f in fields:
            n += 1
            out.append((f"upload:{n}", f, {"asset_id": c.asset_id, "path": a.get("path"), "field": f,
                                           "crop": PMAX_CROPS[f][0], "media": "image"}))
    for pid in (spec.google or {}).get("logos") or []:
        a = assets.get(f"logo:{pid}") or {}
        n += 1
        f = a.get("field") or "LOGO"
        out.append((f"upload:{n}", f, {"photo_id": pid, "path": a.get("path"), "field": f, "crop": "logo", "media": "image"}))
    return out


def plan_steps(spec, conn_row, assets: dict) -> list:
    """The ledger for a Google launch: (PMax) one upload per image asset, then one mutate that makes everything
    else, then a GAQL check of what Google now holds. No call is made here."""
    customer = str(conn_row["external_account_id"])
    g = spec.google or {}
    steps = []
    if g.get("type") == "pmax":
        ups = plan_uploads(spec, assets)
        for key, field, req in ups:
            steps.append({"key": key, "kind": "upload", "name": f"{spec.name} · {FIELD_WORDS.get(field, 'image')}",
                          "request": req})
        ops = pmax_operations(spec, customer, [(k, f) for k, f, _ in ups])
    else:
        ops = search_operations(spec, customer)
    steps.append({"key": "campaign", "kind": "campaign", "name": spec.name,
                  "request": {"type": g.get("type") or "search", "operations": ops}})
    steps.append({"key": "check", "kind": "check", "name": f"{spec.name} · check",
                  "request": {"type": g.get("type") or "search"}})
    return steps


# ---------------------------------------------------------------- running a step

def _fill(value, results: dict, lid):
    """"{lid}" in names -> the launch id; "$upload:n" -> that upload's resource name."""
    if isinstance(value, dict):
        return {k: _fill(v, results, lid) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, results, lid) for v in value]
    if isinstance(value, str):
        if value.startswith("$"):
            r = results.get(value[1:]) or {}
            if not r.get("resource_name"):
                raise KeyError(f"step {value[1:]} has no result yet")
            return r["resource_name"]
        return value.replace("{lid}", str(int(lid)))
    return value


def _ctx_access(ctx: dict) -> str:
    return access_token(ctx["token"])


def _find_campaign(ctx: dict, access: str):
    """The campaign of this launch on Google (by its tagged name), or None. Removed ones don't count."""
    name_tail = tag(ctx["launch_id"], "campaign")
    rows = search(access, ctx["account"], "SELECT campaign.resource_name, campaign.id, campaign.name, campaign.status "
                  f"FROM campaign WHERE campaign.name LIKE {gaql_str('%' + _like_escape(name_tail))} "
                  "AND campaign.status != 'REMOVED'", login=ctx.get("login"), max_wait=float(ctx.get("max_wait") or 0))
    for r in rows:
        c = r.get("campaign") or {}
        if str(c.get("name") or "").endswith(name_tail):
            return c
    return None


def _like_escape(s: str) -> str:
    """GAQL LIKE: [, ], % and _ are matched literally inside [ ]."""
    return re.sub(r"([\[\]%_])", r"[\1]", s)


def _objects_of(ctx, access, camp_rn: str, typ: str) -> list:
    """What one launch mutate made under a campaign (for ad_objects), read back with GAQL."""
    q = (f"SELECT ad_group.resource_name, ad_group.id, ad_group.name, ad_group_ad.resource_name, ad_group_ad.ad.id "
         f"FROM ad_group_ad WHERE campaign.resource_name = {gaql_str(camp_rn)}") if typ == "search" else \
        (f"SELECT asset_group.resource_name, asset_group.id, asset_group.name FROM asset_group "
         f"WHERE campaign.resource_name = {gaql_str(camp_rn)}")
    out = []
    for r in search(access, ctx["account"], q, login=ctx.get("login")):
        if typ == "search":
            ag, ad = r.get("adGroup") or {}, r.get("adGroupAd") or {}
            if ag.get("id") and not any(o["external_id"] == str(ag["id"]) for o in out):
                out.append({"kind": "adgroup", "external_id": str(ag["id"]), "name": ag.get("name"), "parent": "campaign"})
            if (ad.get("ad") or {}).get("id"):
                out.append({"kind": "ad", "external_id": str(ad["ad"]["id"]), "name": "Responsive search ad",
                            "parent": "adgroup"})
        else:
            ag = r.get("assetGroup") or {}
            if ag.get("id"):
                out.append({"kind": "assetgroup", "external_id": str(ag["id"]), "name": ag.get("name"), "parent": "campaign"})
    return out


def _id_of(rn: str) -> str:
    return str(rn or "").rsplit("/", 1)[-1].split("~")[-1]


def mutate(ctx: dict, access: str, operations: list, validate_only: bool = False) -> dict:
    return _call("POST", f"/customers/{ctx['account']}/googleAds:mutate", access, login=ctx.get("login"),
                 body={"mutateOperations": operations, "partialFailure": False, "validateOnly": validate_only},
                 account=str(ctx["account"]), retries=CREATE_RETRIES, max_wait=float(ctx.get("max_wait") or 0))


DUPLICATE = {"campaignError.DUPLICATE_CAMPAIGN_NAME", "campaignBudgetError.DUPLICATE_NAME",
             "adGroupError.DUPLICATE_ADGROUP_NAME", "assetGroupError.DUPLICATE_NAME", "assetError.DUPLICATE_ASSET_NAME"}


def _campaign_step(step: dict, ctx: dict, access: str) -> dict:
    typ = step["request"].get("type") or "search"
    if ctx.get("lookup"):
        found = _find_campaign(ctx, access)
        if found:
            return _found(ctx, access, found, typ)
    ops = _fill(step["request"]["operations"], ctx["results"], ctx["launch_id"])
    try:
        res = mutate(ctx, access, ops)
    except adhttp.AdHttpError as e:
        if e.code in DUPLICATE:              # an earlier run made it and its answer was lost
            found = _find_campaign(ctx, access)
            if found:
                return _found(ctx, access, found, typ)
        raise
    answers = res.get("mutateOperationResponses") or []
    objs, camp_rn = [], None
    for op, ans in zip(ops, answers):
        kind = next(iter(op))
        rn = next(iter(ans.values()), {}).get("resourceName") if isinstance(ans, dict) and ans else None
        if kind == "campaignOperation":
            camp_rn = rn
        elif kind == "adGroupOperation":
            objs.append({"kind": "adgroup", "external_id": _id_of(rn), "name": op[kind]["create"].get("name"),
                         "parent": "campaign"})
        elif kind == "adGroupAdOperation":
            objs.append({"kind": "ad", "external_id": _id_of(rn), "name": "Responsive search ad", "parent": "adgroup"})
        elif kind == "assetGroupOperation":
            objs.append({"kind": "assetgroup", "external_id": _id_of(rn), "name": op[kind]["create"].get("name"),
                         "parent": "campaign"})
    if not camp_rn:
        raise adhttp.AdHttpError("google", "no campaign in the mutate answer")
    return {"id": _id_of(camp_rn), "resource_name": camp_rn, "objects": objs, "object": "campaign"}


def _found(ctx, access, camp: dict, typ: str) -> dict:
    rn = camp.get("resourceName") or f"customers/{ctx['account']}/campaigns/{camp.get('id')}"
    return {"id": str(camp.get("id") or _id_of(rn)), "resource_name": rn, "found": True, "object": "campaign",
            "objects": _objects_of(ctx, access, rn, typ)}


def _check_step(step: dict, ctx: dict, access: str) -> dict:
    """GAQL: the campaign is there and PAUSED, and what Google says about the ad or asset group so far."""
    camp = (ctx["results"].get("campaign") or {})
    rn = camp.get("resource_name")
    if not rn:
        raise KeyError("step campaign has no result yet")
    typ = step["request"].get("type") or "search"
    if typ == "search":
        q = ("SELECT campaign.id, campaign.status, campaign.serving_status, ad_group_ad.status, "
             "ad_group_ad.policy_summary.approval_status, ad_group_ad.policy_summary.review_status "
             f"FROM ad_group_ad WHERE campaign.resource_name = {gaql_str(rn)}")
    else:
        q = ("SELECT campaign.id, campaign.status, campaign.serving_status, asset_group.status, "
             f"asset_group.primary_status FROM asset_group WHERE campaign.resource_name = {gaql_str(rn)}")
    rows = search(access, ctx["account"], q, login=ctx.get("login"))
    if not rows:
        raise adhttp.AdHttpError("google", "the campaign isn't readable on Google yet")
    c = rows[0].get("campaign") or {}
    if str(c.get("status") or "") != "PAUSED":
        # never left running: pause it (it can only be ENABLED if someone changed it in Google Ads meanwhile)
        _set_paused(ctx, access, rn)
    first = rows[0]
    pol = ((first.get("adGroupAd") or {}).get("policySummary") or {})
    return {"id": "", "campaign_status": "PAUSED", "serving_status": c.get("servingStatus"),
            "approval": pol.get("approvalStatus") or (first.get("assetGroup") or {}).get("primaryStatus"),
            "review": pol.get("reviewStatus")}


def _set_paused(ctx, access, camp_rn: str):
    _call("POST", f"/customers/{ctx['account']}/campaigns:mutate", access, login=ctx.get("login"),
          body={"operations": [{"update": {"resourceName": camp_rn, "status": "PAUSED"}, "updateMask": "status"}]},
          account=str(ctx["account"]), max_wait=float(ctx.get("max_wait") or 0))


def _image_bytes(req: dict) -> bytes:
    """The bytes to upload for one PMax image step: the batch image cropped to the field's size (cached next to
    it, like the launch kit's exports), or the logo as it is when square or 4:1, else padded to a square."""
    import exporter
    src = req["path"]
    if req.get("crop") == "logo":
        return logo_bytes(src)
    f = PMAX_CROPS[req["field"]]
    dst = exporter.target_path(src, "google", f[0])
    if not os.path.exists(dst):
        exporter.export_image(src, dst, f[1], f[2])
    with open(dst, "rb") as fh:
        return fh.read()


def logo_kind(path: str):
    """("LOGO" | "LANDSCAPE_LOGO", padded?) for a logo file, or None when it is too small to use."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        return None
    if not w or not h:
        return None
    r = w / h
    if abs(r - 4.0) / 4.0 <= 0.02 and w >= 512 and h >= 128:
        return "LANDSCAPE_LOGO", False
    if abs(r - 1.0) <= 0.02:
        return ("LOGO", False) if min(w, h) >= LOGO_MIN else None
    return ("LOGO", True) if max(w, h) >= LOGO_MIN else None


def logo_bytes(path: str) -> bytes:
    import io
    from PIL import Image
    kind = logo_kind(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    if not kind or not kind[1]:
        return raw
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGBA")
        side = max(im.size)
        canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
        canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
        out = io.BytesIO()
        canvas.convert("RGB").save(out, "PNG")
        return out.getvalue()


def _find_asset(ctx, access, name: str):
    rows = search(access, ctx["account"], "SELECT asset.resource_name, asset.id, asset.name FROM asset "
                  f"WHERE asset.name = {gaql_str(name)}", login=ctx.get("login"))
    return (rows[0].get("asset") if rows else None) or None


def _upload(step: dict, ctx: dict) -> dict:
    access = _ctx_access(ctx)
    name = _named(step["name"], step["key"]).replace("{lid}", str(int(ctx["launch_id"])))[:120]
    if ctx.get("lookup"):
        found = _find_asset(ctx, access, name)
        if found:
            return {"id": str(found.get("id") or _id_of(found.get("resourceName"))),
                    "resource_name": found.get("resourceName"), "object": "asset", "found": True}
    data = base64.b64encode(_image_bytes(step["request"])).decode()
    try:
        res = _call("POST", f"/customers/{ctx['account']}/assets:mutate", access, login=ctx.get("login"),
                    body={"operations": [{"create": {"name": name, "type": "IMAGE", "imageAsset": {"data": data}}}]},
                    account=str(ctx["account"]), retries=CREATE_RETRIES, max_wait=float(ctx.get("max_wait") or 0))
    except adhttp.AdHttpError as e:
        if e.code in DUPLICATE:
            found = _find_asset(ctx, access, name)
            if found:
                return {"id": str(found.get("id")), "resource_name": found.get("resourceName"), "object": "asset",
                        "found": True}
        raise
    rn = ((res.get("results") or [{}])[0] or {}).get("resourceName")
    if not rn:
        raise adhttp.AdHttpError("google", "no resource name in the image upload")
    return {"id": _id_of(rn), "resource_name": rn, "object": "asset"}


def execute_step(step: dict, ctx: dict) -> dict:
    """Run one step. ctx: token (the refresh token), account (customer id), login (manager id or None),
    launch_id, results, lookup. Returns {"id", "resource_name", ...}; raises adhttp errors."""
    if step["kind"] == "upload":
        return _upload(step, ctx)
    access = _ctx_access(ctx)
    if step["kind"] == "campaign":
        return _campaign_step(step, ctx, access)
    if step["kind"] == "check":
        return _check_step(step, ctx, access)
    raise KeyError(step["kind"])


def compensate(step: dict, ctx: dict) -> str:
    """Undo one done step of a failed launch. Google can't delete an image asset (it stays unused in the
    account's asset library: "kept"); the campaign is left PAUSED and reported ("kept"), as on Meta; the check
    made nothing. Raises when Google refuses."""
    if step["kind"] == "campaign" and step.get("external_id"):
        access = _ctx_access(ctx)
        rn = (step.get("result") or {}).get("resource_name") or f"customers/{ctx['account']}/campaigns/{step['external_id']}"
        rows = search(access, ctx["account"], f"SELECT campaign.status FROM campaign WHERE campaign.resource_name = "
                      f"{gaql_str(rn)}", login=ctx.get("login"), max_wait=float(ctx.get("max_wait") or 0))
        if rows and str((rows[0].get("campaign") or {}).get("status") or "PAUSED") not in ("PAUSED", "REMOVED"):
            _set_paused(ctx, access, rn)
        return "kept"
    if step["kind"] == "check":
        return "compensated"
    return "kept"


def verify(step: dict, ctx: dict) -> bool:
    """A kept object of an earlier run is still on Google (before a retry reuses it)."""
    rn = (step.get("result") or {}).get("resource_name")
    if not step.get("external_id") or not rn:
        return False
    access = _ctx_access(ctx)
    res = "asset" if step["kind"] == "upload" else "campaign"
    rows = search(access, ctx["account"], f"SELECT {res}.resource_name" + (", campaign.status" if res == "campaign" else "")
                  + f" FROM {res} WHERE {res}.resource_name = {gaql_str(rn)}", login=ctx.get("login"))
    if not rows:
        return False
    return res == "asset" or str((rows[0].get("campaign") or {}).get("status") or "") != "REMOVED"


# ---------------------------------------------------------------- report (adinsights.py)

def pull_insights(ctx: dict, objects, day_range) -> list:
    """Daily numbers for the launched campaigns, one GAQL search on campaign by segments.date, as rows in the
    ad_insights_daily shape (campaign level). ctx: token (the refresh token), account (customer id), login, currency.
    day_range: (first, last) in the customer's own dates (Google's segments.date is the account's time zone).
    cost_micros is in the account's currency; conversions and their value are Google's primary conversions."""
    ids = sorted({re.sub(r"\D", "", str(o.get("external_id") or "")) for o in objects if o.get("kind") == "campaign"}
                 - {""})
    if not ids:
        return []
    first, last = (str(d)[:10] for d in day_range)
    if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", first) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", last)):
        raise ValueError("day_range must be two YYYY-MM-DD dates")
    cur = ctx.get("currency") or "USD"
    per_minor = 1_000_000 // adspec.minor_factor(cur)
    q = ("SELECT campaign.id, segments.date, metrics.cost_micros, metrics.impressions, metrics.clicks, "
         "metrics.conversions, metrics.conversions_value FROM campaign "
         f"WHERE campaign.id IN ({', '.join(ids)}) AND segments.date BETWEEN '{first}' AND '{last}'")
    out = []
    for r in search(_ctx_access(ctx), ctx["account"], q, login=ctx.get("login"),
                    max_wait=float(ctx.get("max_wait") or 0)):
        c, seg, m = r.get("campaign") or {}, r.get("segments") or {}, r.get("metrics") or {}

        def num(k, d=0.0):
            try:
                return float(m.get(k) if m.get(k) not in (None, "") else d)
            except (TypeError, ValueError):
                return d
        out.append({"platform": "google", "external_object_id": str(c.get("id") or ""), "level": "campaign",
                    "day": str(seg.get("date") or "")[:10], "currency": cur,
                    "spend_minor": int(num("costMicros")) // per_minor, "impressions": int(num("impressions")),
                    "clicks": int(num("clicks")), "link_clicks": int(num("clicks")),
                    "conversions": int(round(num("conversions"))),
                    "conversion_value_minor": int(round(num("conversionsValue") * adspec.minor_factor(cur))),
                    "raw_json": json.dumps(m, separators=(",", ":"))[:2000]})
    return out


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

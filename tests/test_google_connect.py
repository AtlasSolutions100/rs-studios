"""Connecting a Google Ads account (roadmap M3.1) on a throwaway database. Google is the fake REST endpoint and
OAuth server in tests/fakegoogleads.py (answers shaped like tests/fixtures/google/*.json, hand-written from the
Google Ads API v25 REST reference, to re-record with tools/google_sandbox_launch.py): no request leaves the machine.

    python tests/test_google_connect.py      (tests/test_app.py runs it too)

  * GOOGLE_ADS off (the default) or AD_LAUNCH off: no Google button, every Google route is a 404
  * the OAuth start: the adwords scope, access_type=offline, prompt=consent, PKCE S256, state, fixed redirect URI
  * the callback: bad or reused state, another person, cancelled, the adwords box left unticked, no refresh token,
    a login with no Google Ads account; the refresh token goes only into the token box
  * the customer picker: accessible customers, a manager's whole hierarchy (clients reached through the manager
    get it as login-customer-id), managers and cancelled accounts listed but not pickable, test accounts marked
  * reconnecting the same customer updates the row; another Google login's old grant is revoked
  * Disconnect revokes the refresh token at Google; an unfinished connect is swept and revoked
  * adhttp's Google errors: every code the adapter handles maps to the right kind (Reconnect, permission, wait,
    retry, fail), JSON bodies, retries on 5xx and CONCURRENT_MODIFICATION, never on 401
  * no refresh or access token, client secret, code or PKCE verifier in any log line, page or the database file
"""
import atexit
import base64
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="rs_gconn_")
atexit.register(shutil.rmtree, TMP, True)
CLIENT_SECRET = "GOCSPX-fake-client-secret-3141"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0", RS_LOG_JSON="1",
                  RS_DISK_MIN_GB="0.2", TOKEN_KEY=base64.b64encode(b"G" * 32).decode(),
                  GOOGLE_OAUTH_CLIENT_ID="555-fake.apps.googleusercontent.com", GOOGLE_OAUTH_CLIENT_SECRET=CLIENT_SECRET)
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "GOOGLE_ADS", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET",
          "TOKEN_KEYS_OLD", "META_APP_ID", "META_APP_SECRET", "META_LOGIN_CONFIG_ID"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adplatforms  # noqa: E402
import db  # noqa: E402
from adfake import fixture  # noqa: E402
from adplatforms import google  # noqa: E402
from fakegoogleads import FakeGoogleAds  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage() + (logging.Formatter().formatException(record.exc_info)
                                                     if record.exc_info else ""))
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


CAP = Capture()
for name in ("", "campaignify", "rs", "werkzeug", "urllib3"):
    lg = logging.getLogger(name)
    lg.addHandler(CAP)
    lg.setLevel(logging.DEBUG)
T = [time.time()]
adhttp.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.52.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return c, u


def q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def html(r):
    return r.get_data(as_text=True)


def flashes(c):
    with c.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


F = FakeGoogleAds()
F.customer("1112223334", "Ember Shop")
F.customer("9998887776", "Ember Agency MCC", manager=True)
F.customer("5556667778", "Ember West", parent="9998887776", currency="CAD", tz="America/Vancouver")
F.customer("4445556667", "Sub MCC", manager=True, parent="9998887776")
F.customer("3334445556", "Ember Deep", parent="4445556667", test=True)
F.customer("2223334445", "Old Shop", status="CANCELED")
adhttp.TRANSPORT = F
owner, u = signup("gconn-owner@example.com")
WS = u["workspace_id"]

# ================================================================ 1. the flags
print("flags")
check(owner.post("/connect/google", data={"csrf": csrf(owner)}).status_code == 404, "AD_LAUNCH unset: /connect/google is a 404")
os.environ["AD_LAUNCH"] = "on"
page = html(owner.get("/app/settings/ad-accounts"))
check("Connect Google Ads" not in page and "data-google-checklist" not in page,
      "AD_LAUNCH on, GOOGLE_ADS unset (the default): Settings shows no Google")
check(owner.post("/connect/google", data={"csrf": csrf(owner)}).status_code == 404
      and owner.get("/connect/google/callback?state=x&code=y").status_code == 404,
      "GOOGLE_ADS unset: the start and the callback are 404")
os.environ["GOOGLE_ADS"] = "1"
os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET")
page = html(owner.get("/app/settings/ad-accounts"))
check("Connecting Google Ads isn't switched on yet." in page and "Connect Google Ads</button>" not in page,
      "GOOGLE_ADS on without the OAuth client secret: says it isn't switched on, no button")
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = CLIENT_SECRET
page = html(owner.get("/app/settings/ad-accounts"))
check("Connect Google Ads</button>" in page and "data-google-checklist" in page and "Set up billing" in page
      and "Campaignify never sees your card" in page and "https://ads.google.com/aw/billing/settings" in page,
      "both flags on: the Connect Google Ads button and the create-and-bill checklist with links into Google Ads")
check(not re.search(r"[–—]", re.sub(r"<[^>]+>", " ", page)), "no em or en dashes on Settings")

# ================================================================ 2. the OAuth start
print("start")


def start(c, **form):
    r = c.post("/connect/google", data={"csrf": csrf(c), **form})
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    return loc, {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}


loc, qs = start(owner)
check(loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?"), "start redirects to Google's OAuth 2.0 endpoint")
check(qs["scope"].split() == ["openid", "email", "https://www.googleapis.com/auth/adwords"],
      "the scope is openid, email and https://www.googleapis.com/auth/adwords")
check(qs["access_type"] == "offline" and qs["prompt"] == "consent", "access_type=offline and prompt=consent (a refresh token every time)")
check(qs["redirect_uri"] == "https://campaignify.test/connect/google/callback" and qs["client_id"].startswith("555-fake"),
      "the redirect URI is RS_PUBLIC_URL + /connect/google/callback; the sign-in client id is reused")
check(qs["code_challenge_method"] == "S256" and len(qs["code_challenge"]) == 43 and len(qs["state"]) >= 24,
      "PKCE S256 and a random state")
os.environ["GOOGLE_ADS_CLIENT_ID"] = "777-ads.apps.googleusercontent.com"
os.environ["GOOGLE_ADS_CLIENT_SECRET"] = "GOCSPX-other"
_, qs2 = start(owner)
check(qs2["client_id"].startswith("777-ads"), "GOOGLE_ADS_CLIENT_ID overrides the client when set")
os.environ.pop("GOOGLE_ADS_CLIENT_ID")
os.environ.pop("GOOGLE_ADS_CLIENT_SECRET")
member, mu = signup("gconn-member@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET workspace_id=?, role='member' WHERE id=?", (WS, mu["id"]))
    conn.commit()
check(member.post("/connect/google", data={"csrf": csrf(member)}).status_code == 403, "a member can't start a connect (403)")

# ================================================================ 3. the callback
print("callback")
loc, qs = start(owner)
r = owner.get(f"/connect/google/callback?state=wrong&code=c1")
check(r.status_code == 302 and "expired" in flashes(owner) and not q("SELECT 1 FROM ad_connections"),
      "a wrong state is refused, nothing stored")
loc, qs = start(owner)
r = owner.get(f"/connect/google/callback?error=access_denied&state={qs['state']}")
check("cancelled" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "a cancelled consent changes nothing")
loc, qs = start(owner)
F.code("code-noscope", access=["1112223334"], scope="openid https://www.googleapis.com/auth/userinfo.email")
r = owner.get(f"/connect/google/callback?state={qs['state']}&code=code-noscope")
check("tick the Google Ads box" in flashes(owner) and not q("SELECT 1 FROM ad_connections"),
      "the Google Ads box left unticked on Google's screen: told to tick it, nothing stored")
loc, qs = start(owner)
F.code("code-norefresh", access=["1112223334"], refresh=False)
owner.get(f"/connect/google/callback?state={qs['state']}&code=code-norefresh")
check("lasting permission" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "no refresh token: refused, nothing stored")
loc, qs = start(owner)
F.code("code-nouser", access=[])
F.fail("customers:listAccessibleCustomers", "google/error_not_ads_user.json")
owner.get(f"/connect/google/callback?state={qs['state']}&code=code-nouser")
check("no Google Ads account" in flashes(owner) and not q("SELECT 1 FROM ad_connections"),
      "a Google login with no Google Ads account (NOT_ADS_USER): plain words, nothing stored")
other, ou = signup("gconn-other@example.com")
loc, qs = start(owner)
r = other.get(f"/connect/google/callback?state={qs['state']}&code=x")
check("expired" in flashes(other), "another person can't use this person's state")
check(owner.get(f"/connect/google/callback?state={qs['state']}&code=x").status_code == 302 and "expired" in flashes(owner),
      "and the state doesn't work twice")
logged_out = client()
check(logged_out.get("/connect/google/callback?state=a&code=b").headers["Location"].endswith("/login"),
      "logged out: sent to log in, without the code in any next link")

loc, qs = start(owner)
F.code("code-good", sub="108700000000000000001", email="owner@embershop.example",
       access=["1112223334", "9998887776", "2223334445"])
F.calls.clear()
r = owner.get(f"/connect/google/callback?state={qs['state']}&code=code-good")
row = q("SELECT * FROM ad_connections WHERE platform='google'")[0]
check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/settings/ad-accounts/{row['id']}/pick"),
      "a good code: a pending connection, then the picker")
REFRESH = list(F.refresh)[-1]
ACCESS = [t for t in F.tokens][-1]
check(row["status"] == "pending" and row["token_enc"].startswith("cfy1:") and REFRESH not in row["token_enc"]
      and adplatforms.open_token(row) == REFRESH, "the refresh token is stored only in the token box")
check(row["token_expires_at"] is None and "https://www.googleapis.com/auth/adwords" in row["scopes"]
      and row["external_user_id"] == "108700000000000000001", "no expiry (refresh tokens last), the scopes, and who connected")
tok = F.find("/token")[0]
check(tok["data"]["code_verifier"] and tok["data"]["redirect_uri"] == "https://campaignify.test/connect/google/callback",
      "the code exchange sends the PKCE verifier and the same redirect URI")
check(not any("developer-token" in {k.lower() for k in c["headers"]} for c in F.calls if c["host"] == "googleads.googleapis.com"),
      "no developer-token header (developer tokens were sunset 2026-09-09; access follows the Cloud project)")
h = [c for c in F.calls if c["path"].endswith("/9998887776/googleAds:search") and "customer_client" in c["data"]["query"]]
check(h and h[0]["headers"].get("login-customer-id") == "9998887776", "a manager's hierarchy is read with login-customer-id")

# ================================================================ 4. the picker
print("customer picker")
page = html(owner.get(f"/app/settings/ad-accounts/{row['id']}/pick"))
check("Signed in with Google as owner@embershop.example" in page, "the picker says which Google login")
for name, ok_ in (("Ember Shop", True), ("Ember West", True), ("Ember Deep", True)):
    m = re.search(r'<input type="radio" name="account" value="(\d+)"[^>]*>\s*<b>' + name, page)
    check(m and "disabled" not in m.group(0), f"{name} can be picked")
for name in ("Ember Agency MCC", "Sub MCC", "Google Ads 222-333-4445"):
    m = re.search(r'<input type="radio" name="account" value="(\d+)"[^>]*>\s*<b>' + name, page)
    check(m and "disabled" in m.group(0), f"{name} is listed but can't be picked (a manager, or cancelled)")
check("555-666-7778 · CAD" in page and "through Ember Agency MCC" in page, "a manager's client shows its id, currency and the manager")
check("333-444-5556" in page and "Test account" in page, "a client two managers down is listed too, marked as a test account")
check("Cancelled on Google Ads" in page, "a cancelled account (Google won't even give its name) says so")
r = owner.post(f"/app/settings/ad-accounts/{row['id']}/pick", data={"csrf": csrf(owner), "account": "9998887776"})
check(q("SELECT status FROM ad_connections WHERE id=?", row["id"])[0]["status"] == "pending",
      "picking a manager is refused")
r = owner.post(f"/app/settings/ad-accounts/{row['id']}/pick", data={"csrf": csrf(owner), "account": "5556667778"})
row = q("SELECT * FROM ad_connections WHERE id=?", row["id"])[0]
check(row["status"] == "active" and row["external_account_id"] == "5556667778" and row["business_id"] == "9998887776"
      and row["currency"] == "CAD" and row["timezone"] == "America/Vancouver" and row["account_name"] == "Ember West"
      and row["pending_json"] is None, "picking a client stores it with its manager as login-customer-id, currency and zone")
page = html(owner.get("/app/settings/ad-accounts"))
check("Google Ads account 555-666-7778 · CAD" in page and "Reached through manager account 999-888-7776" in page,
      "Settings lists the Google connection")
check(member.get(f"/app/settings/ad-accounts/{row['id']}/pick").status_code == 403, "a member can't pick")
check(other.post(f"/app/settings/ad-accounts/{row['id']}/disconnect", data={"csrf": csrf(other)}).status_code == 404,
      "another workspace's connection is a 404")
G1 = row["id"]

# a direct client: no login-customer-id
loc, qs = start(owner)
F.code("code-direct", sub="108700000000000000001", access=["1112223334"])
owner.get(f"/connect/google/callback?state={qs['state']}&code=code-direct")
p2 = q("SELECT * FROM ad_connections WHERE status='pending' AND platform='google'")[0]
owner.post(f"/app/settings/ad-accounts/{p2['id']}/pick", data={"csrf": csrf(owner), "account": "1112223334"})
row2 = q("SELECT * FROM ad_connections WHERE id=?", p2["id"])[0]
check(row2["status"] == "active" and row2["business_id"] is None, "a customer the login reaches directly needs no manager")

# ================================================================ 5. reconnect and disconnect
print("reconnect and disconnect")
loc, qs = start(owner, reconnect=str(G1))
F.code("code-re", sub="108700000000000000002", email="partner@embershop.example", access=["9998887776"])
owner.get(f"/connect/google/callback?state={qs['state']}&code=code-re")
p3 = q("SELECT * FROM ad_connections WHERE status='pending' AND platform='google'")[0]
page = html(owner.get(f"/app/settings/ad-accounts/{p3['id']}/pick"))
check(re.search(r'value="5556667778" checked', page), "Reconnect preselects the same account")
old_refresh = adplatforms.open_token(row)
owner.post(f"/app/settings/ad-accounts/{p3['id']}/pick", data={"csrf": csrf(owner), "account": "5556667778"})
rows = q("SELECT * FROM ad_connections WHERE platform='google' AND external_account_id='5556667778'")
check(len(rows) == 1 and rows[0]["id"] == G1 and rows[0]["external_user_id"] == "108700000000000000002",
      "reconnecting the same customer updates the same row (one connection per customer)")
check(old_refresh in F.refresh and not F.find("/revoke"),
      "another Google login took it over; the old login's grant stays, since another connection here uses it")
F.calls.clear()
new_refresh = adplatforms.open_token(rows[0])
r = owner.post(f"/app/settings/ad-accounts/{G1}/disconnect", data={"csrf": csrf(owner)})
rv = F.find("/revoke")
check(len(rv) == 1 and rv[0]["data"]["token"] == new_refresh and new_refresh not in F.refresh,
      "Disconnect revokes the refresh token at Google")
gone = q("SELECT * FROM ad_connections WHERE id=?", G1)[0]
check(gone["status"] == "revoked" and gone["token_enc"] is None and "removed Campaignify's access on Google" in flashes(owner),
      "and drops it here")
F.calls.clear()
direct = adplatforms.open_token(row2)
owner.post(f"/app/settings/ad-accounts/{row2['id']}/disconnect", data={"csrf": csrf(owner)})
check(len(F.find("/revoke")) == 1 and direct not in F.refresh, "the last connection of a Google login revokes it")

# an unfinished connect is swept and its grant revoked
loc, qs = start(owner)
F.code("code-abandon", sub="108700000000000000009", access=["1112223334"])
owner.get(f"/connect/google/callback?state={qs['state']}&code=code-abandon")
pa = q("SELECT * FROM ad_connections WHERE status='pending' AND platform='google'")[0]
ab = adplatforms.open_token(pa)
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (pa["id"],))
    conn.commit()
adplatforms._last_sweep[0] = 0
adplatforms.maybe_sweep()
check(not q("SELECT 1 FROM ad_connections WHERE id=?", pa["id"]) and ab not in F.refresh,
      "an unfinished Google connect is dropped after an hour and its grant revoked")

# ================================================================ 6. adhttp and Google's errors
print("Google errors in adhttp")
F2 = FakeGoogleAds()
adhttp.TRANSPORT = F2
F2.customer("1112223334", "Ember Shop")
F2.code("c", access=["1112223334"])
got = F2._oauth("/token", {"grant_type": "authorization_code", "code": "c", "code_verifier": "v",
                           "redirect_uri": "https://campaignify.test/connect/google/callback"}).body
acc = got["access_token"]
KINDS = {
    "error_oauth_token_expired.json": (adhttp.TokenInvalid, False), "error_oauth_token_revoked.json": (adhttp.TokenInvalid, False),
    "error_not_ads_user.json": (adhttp.PermissionDenied, False),
    "error_user_permission_denied.json": (adhttp.PermissionDenied, False),
    "error_customer_not_enabled.json": (adhttp.PermissionDenied, False),
    "error_cloud_project_not_approved.json": (adhttp.PermissionDenied, False),
    "error_developer_token_not_approved.json": (adhttp.PermissionDenied, False),
    "error_action_not_permitted.json": (adhttp.PermissionDenied, False),
    "error_invalid_login_customer.json": (adhttp.AdHttpError, False),
    "error_quota.json": (adhttp.RateLimited, True), "error_quota_temporary.json": (adhttp.RateLimited, True),
    "error_internal.json": (adhttp.AdHttpError, True), "error_transient.json": (adhttp.AdHttpError, True),
    "error_concurrent_modification.json": (adhttp.AdHttpError, True),
    "error_duplicate_campaign_name.json": (adhttp.AdHttpError, False), "error_policy_finding.json": (adhttp.AdHttpError, False),
    "error_policy_violation_keyword.json": (adhttp.AdHttpError, False), "error_budget_too_small.json": (adhttp.AdHttpError, False),
    "error_pmax_business_name.json": (adhttp.AdHttpError, False), "error_pmax_logo.json": (adhttp.AdHttpError, False),
    "error_not_enough_headline.json": (adhttp.AdHttpError, False),
    "error_not_enough_long_headline.json": (adhttp.AdHttpError, False),
    "error_not_enough_description.json": (adhttp.AdHttpError, False),
    "error_not_enough_marketing_image.json": (adhttp.AdHttpError, False),
    "error_not_enough_square_marketing_image.json": (adhttp.AdHttpError, False),
    "error_image_aspect.json": (adhttp.AdHttpError, False), "error_not_allowlisted.json": (adhttp.PermissionDenied, False)
    if "notAllowlistedError.CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE" in adhttp.GOOGLE_PERMISSION else (adhttp.AdHttpError, False),
    "error_field_required.json": (adhttp.AdHttpError, False),
}
check(set(KINDS) == {f for f in os.listdir(os.path.join(HERE, "fixtures", "google")) if f.startswith("error_")},
      "every Google error fixture is covered here")
for name, (kind, retry) in KINDS.items():
    F2.calls.clear()
    F2.failures = [["googleAds:search", f"google/{name}", 3]]
    try:
        google.search(acc, "1112223334", "SELECT customer.id FROM customer")
        e = None
    except adhttp.AdHttpError as err:
        e = err
    body = fixture(f"google/{name}")
    cat, val = next(iter(body["error"]["details"][0]["errors"][0]["errorCode"].items()))
    tries = len(F2.calls)
    # a rate limit blocks the customer's budget for Google's retryDelay, so the next try waits (the saga waits it out)
    want = 1 if kind is adhttp.RateLimited else adhttp.RETRIES if retry else 1
    check(type(e) is kind and e.code == f"{cat}.{val}" and e.trace and e.platform == "google" and tries == want,
          f"{cat}.{val}: {kind.__name__}, " + ("the customer waits" if kind is adhttp.RateLimited else
                                               "retried" if retry else "not retried") + f" ({tries} tries)")
    if kind is adhttp.RateLimited:
        T[0] += 301
    words = google.translate(e)
    check(words and not re.search(r"[–—]", words) and "Google" in words or "Performance Max" in words,
          f"{cat}.{val} in plain words: {words[:70]}")
check(google.translate(adhttp.NetworkError("google", "x")) == google.NETWORK_WORDS, "a network error: we couldn't reach Google")
F2.failures = [["googleAds:search", "google/error_quota.json", 1]]
try:
    google.search(acc, "1112223334", "SELECT customer.id FROM customer")
except adhttp.RateLimited as err:
    e = err
check(e.retry_after == 30, "a quota error carries Google's retryDelay (30 s)")
F2.calls.clear()
google.search(acc, "1112223334", "SELECT customer.id FROM customer", max_wait=40)
check(len(F2.calls) == 1, "the next call waits those 30 s, then goes through")
try:
    adhttp.call("google", "POST", google.TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": "nope"})
    e = None
except adhttp.AdHttpError as err:
    e = err
check(isinstance(e, adhttp.TokenInvalid) and e.code == "oauth.invalid_grant" and google.translate(e) == google.RECONNECT,
      "a revoked or expired refresh token (invalid_grant) means Reconnect")
F2.fail("/token", "google/oauth_invalid_client.json")
try:
    google.access_token("1//whatever-refresh-value")
    e = None
except adhttp.AdHttpError as err:
    e = err
check(type(e) is adhttp.AdHttpError and e.code == "oauth.invalid_client", "a bad OAuth client is our configuration, not the person's")
F2.calls.clear()
google.search(acc, "1112223334", "SELECT customer.id FROM customer")
c0 = F2.calls[-1]
check(c0["headers"]["Content-Type"] == "application/json" and isinstance(c0["raw"], str) and '"query"' in c0["raw"]
      and c0["headers"]["Authorization"] == f"Bearer {acc}", "GAQL goes as a JSON body with the bearer token")
check(google.dashed("1234567890") == "123-456-7890", "customer ids are shown dashed, the way Google Ads shows them")

# ================================================================ 7. no secrets anywhere
print("secrets")
logs = "\n".join(CAP.lines)
secrets_ = [REFRESH, new_refresh, old_refresh, ab, ACCESS, acc, CLIENT_SECRET, "code-good", "code-direct"]
check(all(s not in logs for s in secrets_), "no refresh token, access token, client secret or code in any log line")
check(not any(v in logs for v in [c["data"].get("code_verifier") for c in F.calls if c["data"].get("code_verifier")]),
      "no PKCE verifier in the logs")
raw = open(db.DB_PATH, "rb").read()
check(all(s.encode() not in raw for s in secrets_[:6]), "no token in the database file")
page = html(owner.get("/app/settings/ad-accounts"))
check(all(s not in page for s in secrets_), "no token on the Settings page")
print(f"\nall {passed} checks passed")

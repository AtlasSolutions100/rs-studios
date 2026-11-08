"""Connected ad accounts (roadmap M2.1) on a throwaway database. Meta is faked (tests/adfake.py and
tests/fixtures/meta/*.json, hand-written from Meta's reference shapes for v26.0, not recorded):
no request leaves the machine.

    python tests/test_adconnect.py      (tests/test_app.py runs it too)

  * the token box (secretsbox.py): seal/open, context binding, tampering, key rotation, no key
  * adhttp.py: retries (5xx, transient codes), 190 is not retried, scrubbed network errors,
    the point budget, Meta's usage headers, the 30 s edit lock
  * AD_LAUNCH off / beta / on, and Meta not configured
  * the connect flow: start (PKCE, state, config_id, fixed redirect URI), callback checks (state,
    other person, expired, cancelled, logged out), the long-lived exchange (USER) and none
    (SYSTEM_USER), pick account and Page (+ Instagram), pick pixel, settings list
  * scoping: another workspace's connection is a 404; members can look but not connect
  * Reconnect after a 190 updates the same row; Disconnect revokes at Meta unless the Meta login
    is still used by another connection
  * no token, app secret, code or PKCE verifier in any log line, page or the database file
"""
import atexit
import base64
import json
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
TMP = tempfile.mkdtemp(prefix="rs_adconn_")
atexit.register(shutil.rmtree, TMP, True)
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
KEY1 = base64.b64encode(b"k" * 32).decode()
KEY2 = base64.b64encode(b"n" * 32).decode()
APP_SECRET = "fake-app-secret-9f8e7d6c5b"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_LOG_JSON="1",
                  META_APP_ID="1000000000000001", META_APP_SECRET=APP_SECRET, META_LOGIN_CONFIG_ID="424242424242")
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "AD_RATE_TIER", "TOKEN_KEY", "TOKEN_KEYS_OLD"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adplatforms  # noqa: E402
import db  # noqa: E402
import secretsbox  # noqa: E402
from adfake import FakeTransport, fixture  # noqa: E402
from adplatforms import meta  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


# ---------------------------------------------------------------- every log record, to look for secrets at the end
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

# a fake clock: adhttp's sleeps move it forward instead of waiting
T = [time.time()]
adhttp.CLOCK = lambda: T[0]


def _sleep(s):
    T[0] += max(0.0, s)


adhttp.SLEEP = _sleep
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.31.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    return c


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                            (email,)).fetchone()


def q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def html(r):
    return r.get_data(as_text=True)


# ================================================================ 1. the token box
print("token box")
check(not secretsbox.configured(), "no TOKEN_KEY: the box is not configured")
try:
    secretsbox.seal("x")
    check(False, "sealing without a key fails")
except secretsbox.NotConfigured:
    check(True, "sealing without a key raises NotConfigured (never stores in the clear)")
os.environ["TOKEN_KEY"] = base64.b64encode(b"short").decode()
check(not secretsbox.configured(), "a TOKEN_KEY that isn't 32 bytes doesn't count")
os.environ["TOKEN_KEY"] = "not base64 !!"
check(not secretsbox.configured(), "a TOKEN_KEY that isn't base64 doesn't count")
os.environ["TOKEN_KEY"] = KEY1
b1, b2 = secretsbox.seal("EAAsecret", "ad_connections:1"), secretsbox.seal("EAAsecret", "ad_connections:1")
check(b1 != b2 and "EAAsecret" not in b1 and b1.startswith("cfy1:"), "each box differs (random nonce) and hides the secret")
check(secretsbox.open_box(b1, "ad_connections:1") == "EAAsecret", "a box opens with its key and context")
for bad, why in ((lambda: secretsbox.open_box(b1, "ad_connections:2")), "another workspace's context"), \
                ((lambda: secretsbox.open_box(b1[:-3] + ("A" if b1[-3] != "A" else "B") + b1[-2:], "ad_connections:1")),
                 "a changed byte"), ((lambda: secretsbox.open_box("plain-token", "")), "a value that isn't a box"):
    try:
        bad()
        check(False, f"{why} must not open")
    except secretsbox.BoxError as e:
        check("EAAsecret" not in str(e), f"{why} doesn't open (BoxError, no secret in it)")
os.environ.update(TOKEN_KEY=KEY2, TOKEN_KEYS_OLD=KEY1)
check(secretsbox.open_box(b1, "ad_connections:1") == "EAAsecret", "after a key change the old key still opens old boxes")
check(secretsbox.needs_rotation(b1), "an old box needs rotation")
b3 = secretsbox.rotate(b1, "ad_connections:1")
check(not secretsbox.needs_rotation(b3) and b3.split(":")[1] != b1.split(":")[1], "rotate re-seals under the new key")
os.environ.pop("TOKEN_KEYS_OLD")
check(secretsbox.open_box(b3, "ad_connections:1") == "EAAsecret", "the rotated box opens without the old key")
try:
    secretsbox.open_box(b1, "ad_connections:1")
    check(False, "an old box without the old key must not open")
except secretsbox.BoxError:
    check(True, "once the old key is gone, boxes it sealed don't open")
os.environ["TOKEN_KEY"] = KEY1

# ================================================================ 2. adhttp
print("adhttp")
fake = FakeTransport()
adhttp.TRANSPORT = fake
check("access_token=***" in adhttp.scrub("https://x/y?access_token=EAAabc&x=1") and
      "EAAabc" not in adhttp.scrub("Authorization: Bearer EAAabc.def"), "scrub hides tokens in URLs and headers")
fake.on("GET", "/flaky", (500, {"error": {"message": "boom", "code": 1}}), {"ok": 1})
check(adhttp.call("meta", "GET", meta.GRAPH + "/flaky") == {"ok": 1} and len(fake.find("GET", "/flaky")) == 2,
      "a 500 is retried and the second answer is used")
fake.on("GET", "/transient", "meta/error_2.json", {"ok": 2})
check(adhttp.call("meta", "GET", meta.GRAPH + "/transient") == {"ok": 2}, "Meta code 2 (transient) is retried")
fake.on("GET", "/expired", "meta/error_190.json")
try:
    adhttp.call("meta", "GET", meta.GRAPH + "/expired")
    check(False, "190 raises")
except adhttp.TokenInvalid as e:
    check(e.code == 190 and len(fake.find("GET", "/expired")) == 1, "190 raises TokenInvalid at once (no retry)")
fake.on("GET", "/perm", "meta/error_200.json")
try:
    adhttp.call("meta", "GET", meta.GRAPH + "/perm")
    check(False, "200 raises")
except adhttp.PermissionDenied:
    check(len(fake.find("GET", "/perm")) == 1, "code 200 raises PermissionDenied, not retried")
import requests  # noqa: E402
leak = "EAAleakyTOKEN12345"
fake.on("GET", "/down", requests.ConnectionError(f"Max retries exceeded with url: /v26.0/x?access_token={leak}&"
                                                 f"client_secret={APP_SECRET}"))
try:
    adhttp.call("meta", "GET", meta.GRAPH + "/down", params={"access_token": leak})
    check(False, "network error raises")
except adhttp.NetworkError as e:
    check(leak not in str(e) and APP_SECRET not in str(e) and e.__cause__ is None and e.__context__ is None,
          "a network error is re-raised without the URL, the token or the secret")
    check(len(fake.find("GET", "/down")) == adhttp.RETRIES, "network errors are retried RETRIES times")
fake.on("GET", "/echo", lambda m, u, p, d, h: (400, {"error": {"message": f"bad token {leak}", "code": 100}}))
try:
    adhttp.call("meta", "GET", meta.GRAPH + "/echo", secrets=[leak])
except adhttp.AdHttpError as e:
    check(leak not in str(e) and leak not in e.user_message, "a platform message echoing a token is scrubbed")

# the point budget (Limited tier: 60 points, we stop at 54; decays 60 points per 300 s)
fake.on("GET", "/act_9/campaigns", {"data": []})
for _ in range(54):
    adhttp.call("meta", "GET", meta.GRAPH + "/act_9/campaigns", account="act_9")
try:
    adhttp.call("meta", "GET", meta.GRAPH + "/act_9/campaigns", account="act_9")
    check(False, "the 55th point is refused")
except adhttp.RateLimited as e:
    check(0 < e.retry_after <= 6 and len(fake.find("GET", "/act_9/campaigns")) == 54,
          "54 reads fit the Limited budget; the next one is refused before any request")
T[0] += 5.1
adhttp.call("meta", "GET", meta.GRAPH + "/act_9/campaigns", account="act_9")
check(len(fake.find("GET", "/act_9/campaigns")) == 55, "5 s later one point has decayed and the call goes")
adhttp.call("meta", "GET", meta.GRAPH + "/act_9/campaigns", account="act_9", max_wait=30)
check(len(fake.find("GET", "/act_9/campaigns")) == 56, "with max_wait the call waits for the budget instead")
try:
    adhttp.call("meta", "POST", meta.GRAPH + "/act_10/campaigns", account="act_10", cost=60)
    check(False, "a call bigger than the whole budget is refused")
except adhttp.RateLimited:
    check(True, "a call bigger than the whole budget is refused")
fake.on("GET", "/act_11/insights", (200, {"data": []}, {
    "x-business-use-case-usage": json.dumps({"11": [{"type": "ads_management", "call_count": 96, "total_cputime": 10,
                                                     "total_time": 10, "estimated_time_to_regain_access": 3}]})}))
adhttp.call("meta", "GET", meta.GRAPH + "/act_11/insights", account="act_11")
try:
    adhttp.call("meta", "GET", meta.GRAPH + "/act_11/insights", account="act_11")
    check(False, "Meta's usage header blocks the account")
except adhttp.RateLimited as e:
    check(170 < e.retry_after <= 181, "Meta's usage header (regain access in 3 min) blocks the account here for 3 min")
fake.on("GET", "/act_12/x", "meta/error_17.json")
try:
    adhttp.call("meta", "GET", meta.GRAPH + "/act_12/x", account="act_12")
    check(False, "code 17 raises RateLimited")
except adhttp.RateLimited:
    check(len(fake.find("GET", "/act_12/x")) == 1, "code 17 blocks the account; the retry is refused by our budget")
adhttp.edit_lock("meta", "120000001")
try:
    adhttp.edit_lock("meta", "120000001")
    check(False, "second edit inside 30 s is refused")
except adhttp.RateLimited as e:
    check(29 < e.retry_after <= 30, "a second edit of the same object inside 30 s is refused")
adhttp.edit_lock("meta", "120000002")
adhttp.edit_lock("meta", "120000001", max_wait=31)
check(True, "another object is free, and the same object is free again after 30 s")

# ================================================================ 3. the flag
print("AD_LAUNCH")
a = signup("owner-a@example.com")
wsa = ws_of("owner-a@example.com")
b = signup("owner-b@example.com")
wsb = ws_of("owner-b@example.com")
check(a.get("/app/settings/ad-accounts").status_code == 404, "AD_LAUNCH unset: the page is a 404")
check(a.post("/connect/meta", data={"csrf": csrf(a)}).status_code == 404, "AD_LAUNCH unset: /connect/meta is a 404")
check(a.get("/connect/meta/callback?code=x&state=y").status_code == 404, "AD_LAUNCH unset: the callback is a 404")
check(">Connections<" not in html(a.get("/app/team")), "AD_LAUNCH unset: no Connections item in the menu")
os.environ.update(AD_LAUNCH="beta", AD_LAUNCH_WORKSPACES=f"{wsa['id']}")
check(a.get("/app/settings/ad-accounts").status_code == 200, "beta: an allowlisted workspace sees the page")
check(b.get("/app/settings/ad-accounts").status_code == 404, "beta: any other workspace gets a 404")
check(">Connections<" in html(a.get("/app/team")) and ">Connections<" not in html(b.get("/app/team")),
      "beta: the menu item shows only for the allowlisted workspace")
os.environ.update(AD_LAUNCH="on")
check(b.get("/app/settings/ad-accounts").status_code == 200, "on: every workspace sees the page")

# Meta not configured (no TOKEN_KEY)
os.environ.pop("TOKEN_KEY")
page = html(a.get("/app/settings/ad-accounts"))
check("isn't switched on yet" in page and "Connect Meta</button>" not in page, "no TOKEN_KEY: no Connect button")
r = a.post("/connect/meta", data={"csrf": csrf(a)})
check(r.status_code == 302 and "facebook.com" not in r.headers["Location"], "no TOKEN_KEY: start doesn't go to Meta")
os.environ["TOKEN_KEY"] = KEY1
page = html(a.get("/app/settings/ad-accounts"))
check("Connect Meta</button>" in page and "Done, connect it" in page and "never sees your card" in page,
      "configured: Connect Meta, the no-account checklist and the card line")
check("https://business.facebook.com/settings/ad-accounts" in page, "the checklist deep-links Business Settings > Ad accounts")

# ================================================================ 4. the connect flow
print("connect")
SHORT, LONG = fixture("meta/oauth_code.json")["access_token"], fixture("meta/oauth_long_lived.json")["access_token"]
CODE = "AQDfakeAuthCode-77777"
fake = FakeTransport()
adhttp.TRANSPORT = fake


def meta_routes(f, debug="meta/debug_token_user.json", pixels="meta/adspixels_two.json", me="meta/me.json"):
    f.on("GET", "/oauth/access_token", lambda m, u, p, d, h: "meta/oauth_long_lived.json"
         if p.get("grant_type") == "fb_exchange_token" else "meta/oauth_code.json")
    f.on("GET", "/debug_token", debug)
    f.on("GET", "/me", me)
    f.on("GET", "/me/adaccounts", lambda m, u, p, d, h: "meta/adaccounts_page2.json" if p.get("after")
         else "meta/adaccounts_page1.json")
    f.on("GET", "/me/accounts", "meta/pages.json")
    f.on("GET", "/500000001", "meta/page_ig_linked.json")
    f.on("GET", "/500000002", "meta/page_ig_none.json")
    f.on("GET", "/adspixels", pixels)
    f.on("DELETE", "/me/permissions", "meta/permissions_delete.json")


meta_routes(fake)


def start(c, **form):
    r = c.post("/connect/meta", data={"csrf": csrf(c), **form})
    assert r.status_code == 302, r.status_code
    return r.headers["Location"]


loc = start(a)
qs = {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}
check(loc.startswith("https://www.facebook.com/v26.0/dialog/oauth?"), "start redirects to Meta's v26.0 login dialog")
check(qs["client_id"] == "1000000000000001" and qs["config_id"] == "424242424242" and "scope" not in qs,
      "the dialog gets the app id and the login configuration id (no scope list)")
check(qs["redirect_uri"] == "https://campaignify.test/connect/meta/callback", "the redirect URI is RS_PUBLIC_URL + /connect/meta/callback")
check(qs["response_type"] == "code" and qs["code_challenge_method"] == "S256" and len(qs["code_challenge"]) == 43,
      "code flow with a PKCE S256 challenge")
with a.session_transaction() as s:
    pend = dict(s["ad_oauth"])
check(pend["state"] == qs["state"] and pend["ws"] == wsa["id"], "the state is kept in this session for this workspace")
VERIFIER = pend["verifier"]
check(a.get("/connect/meta").status_code == 405, "GET /connect/meta is refused (CSRF-safe POST only)")

r = a.get(f"/connect/meta/callback?code={CODE}&state=wrong")
check(r.status_code == 302 and not fake.calls and not q("SELECT * FROM ad_connections"),
      "a wrong state stops before any call to Meta")
check(not a.get(f"/connect/meta/callback?code={CODE}&state={qs['state']}").status_code == 200
      and not fake.calls, "the wrong try spent the state: even the right one is refused now")
loc = start(a)
st = parse_qs(urlsplit(loc).query)["state"][0]
with a.session_transaction() as s:
    VERIFIER = s["ad_oauth"]["verifier"]
r = a.get(f"/connect/meta/callback?code={CODE}&state={st}")
rows = q("SELECT * FROM ad_connections WHERE workspace_id=?", wsa["id"])
check(r.status_code == 302 and "/pick" in r.headers["Location"] and len(rows) == 1 and rows[0]["status"] == "pending",
      "the callback makes a pending connection and sends the owner to pick")
r2 = a.get(f"/connect/meta/callback?code={CODE}&state={st}")
check(len(q("SELECT * FROM ad_connections")) == 1, "replaying the same callback does nothing (state is single use)")
ex = [c for c in fake.find("GET", "/oauth/access_token")]
import hashlib  # noqa: E402
chal = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
check(ex[0]["params"]["code"] == CODE and ex[0]["params"]["code_verifier"] == VERIFIER
      and parse_qs(urlsplit(loc).query)["code_challenge"][0] == chal,
      "the code exchange sends the code and the PKCE verifier that matches the challenge")
check(ex[0]["params"]["redirect_uri"] == "https://campaignify.test/connect/meta/callback",
      "the exchange repeats the exact redirect URI")
check(len(ex) == 2 and ex[1]["params"]["grant_type"] == "fb_exchange_token" and ex[1]["params"]["fb_exchange_token"] == SHORT,
      "a USER token is swapped for a long-lived one")
row = rows[0]
check(adplatforms.open_token(row) == LONG and row["token_enc"] != LONG and LONG not in (row["pending_json"] or ""),
      "only the long-lived token is kept, in a token box; the pick list has no token")
check(row["token_expires_at"] and row["token_expires_at"] > db.now(), "its expiry (about 60 days) is stored")
check(row["external_user_id"] == "10200000000000001" and "ads_management" in row["scopes"], "the Meta user and scopes are stored")
check(all(c["url"].startswith("https://graph.facebook.com/v26.0/") for c in fake.calls), "every call uses Graph v26.0")
check(all(c["method"] == "GET" for c in fake.calls), "connecting makes GET calls only (nothing changes on Meta)")
user_calls = [c for c in fake.calls if c["path"] not in ("/v26.0/oauth/access_token", "/v26.0/debug_token")]
check(user_calls and all(c["headers"].get("Authorization", "").startswith("Bearer ") and "access_token" not in c["params"]
                         and len(c["params"].get("appsecret_proof", "")) == 64 for c in user_calls),
      "user-token calls carry the token in the Authorization header with appsecret_proof, never in the URL")
check(len(fake.find("GET", "/me/adaccounts")) == 2 and fake.find("GET", "/me/adaccounts")[1]["params"].get("after") == "QVFIUjNk",
      "ad accounts are read across pages with the after cursor")

pick_url = r.headers["Location"]
page = html(a.get(pick_url))
check("Glow Co main" in page and "Glow Co tests" in page and "Old shop" in page, "the pick page lists every ad account")
check(re.search(r'value="333000333"[^>]*disabled', page) and "Disabled on Meta" in page, "a disabled account can't be picked")
check("Instagram @glowco" in page and "Glow Co Outlet" in page, "Pages show their linked Instagram account")
check(LONG not in page and SHORT not in page, "no token on the pick page")
r = a.post(pick_url, data={"csrf": csrf(a), "account": "999", "page": "500000001"})
check(r.status_code == 302 and q("SELECT status FROM ad_connections")[0]["status"] == "pending",
      "an account that wasn't listed is refused")
r = a.post(pick_url, data={"csrf": csrf(a), "account": "333000333", "page": ""})
check(q("SELECT status FROM ad_connections")[0]["status"] == "pending", "a disabled account is refused")
r = a.post(pick_url, data={"csrf": csrf(a), "account": "111000111", "page": "500000001"})
cid = rows[0]["id"]
check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/settings/ad-accounts/{cid}/pixel"),
      "with 2 pixels, the owner goes on to pick one")
row = q("SELECT * FROM ad_connections WHERE id=?", cid)[0]
check(row["status"] == "active" and row["external_account_id"] == "111000111" and row["currency"] == "USD"
      and row["business_id"] == "900000001" and row["page_id"] == "500000001"
      and row["instagram_actor_id"] == "17841400000000001", "the connection is active with the account, Page and Instagram")
px_call = fake.find("GET", "/act_111000111/adspixels")
check(len(px_call) == 1, "pixels are read for the picked account only")
page = html(a.get(f"/app/settings/ad-accounts/{cid}/pixel"))
check("Glow Co site" in page and "Old landing pages" in page and "Skip for now" in page, "the pixel page lists both")
r = a.post(f"/app/settings/ad-accounts/{cid}/pixel", data={"csrf": csrf(a), "pixel": "700000002"})
px = q("SELECT * FROM pixels WHERE workspace_id=?", wsa["id"])
check(r.status_code == 302 and len(px) == 1 and px[0]["external_pixel_id"] == "700000002" and px[0]["connection_id"] == cid,
      "the picked pixel is saved for the connection")
page = html(a.get("/app/settings/ad-accounts"))
check("Glow Co main" in page and "Connected</span>" in page and "Instagram @glowco" in page and "Old landing pages" in page,
      "Settings lists the connection with its Page, Instagram and pixel")
check(LONG not in page and SHORT not in page, "no token on the Settings page")
check(a.post(f"/app/settings/ad-accounts/{cid}/pick", data={"csrf": csrf(a), "account": "222000222"}).status_code == 302
      and q("SELECT external_account_id FROM ad_connections WHERE id=?", cid)[0][0] == "111000111",
      "an active connection can't be re-picked to another account")
calls_for_connect = len(fake.calls)
check(calls_for_connect <= 12, f"the whole connect takes {calls_for_connect} calls to Meta (budget friendly)")

# ================================================================ 5. callback edge cases
print("callback checks")
fake.calls.clear()
r = a.get("/connect/meta/callback?error=access_denied&error_reason=user_denied&state=x")
check(r.status_code == 302 and not fake.calls, "Cancel on Meta: back to Settings, nothing called")
loc = start(a)
st = parse_qs(urlsplit(loc).query)["state"][0]
with a.session_transaction() as s:
    d = dict(s["ad_oauth"])
    d["at"] = int(time.time()) - 601
    s["ad_oauth"] = d
r = a.get(f"/connect/meta/callback?code={CODE}&state={st}")
check(not fake.calls and len(q("SELECT * FROM ad_connections")) == 1, "a state older than 10 minutes is refused")
loc = start(a)
st = parse_qs(urlsplit(loc).query)["state"][0]
anon = client()
r = anon.get(f"/connect/meta/callback?code={CODE}&state={st}")
check(r.status_code == 302 and r.headers["Location"].endswith("/login") and CODE not in r.headers["Location"],
      "logged out: to the login page, without the code in the address")
r = b.get(f"/connect/meta/callback?code={CODE}&state={st}")
check(not fake.calls, "another person's browser can't finish someone else's connect")
# a failing exchange: a network error whose message has the code and the secret in it
fake.on("GET", "/oauth/access_token", requests.ConnectionError(
    f"HTTPSConnectionPool: /v26.0/oauth/access_token?client_secret={APP_SECRET}&code={CODE}"))
loc = start(a)
st = parse_qs(urlsplit(loc).query)["state"][0]
r = a.get(f"/connect/meta/callback?code={CODE}&state={st}", follow_redirects=True)
check("work. Try again in a minute" in html(r) and len(q("SELECT * FROM ad_connections")) == 1, "a failed exchange says so and stores nothing")
fake.on("GET", "/oauth/access_token", "meta/error_bad_code.json")
loc = start(a)
st = parse_qs(urlsplit(loc).query)["state"][0]
r = a.get(f"/connect/meta/callback?code={CODE}&state={st}", follow_redirects=True)
check("work. Try again in a minute" in html(r), "a used authorization code (Meta 100) says so")
fake.on("GET", "/debug_token", lambda m, u, p, d, h: {"data": dict(fixture("meta/debug_token_user.json")["data"],
                                                                   app_id="999")})
fake.on("GET", "/oauth/access_token", "meta/oauth_code.json")
loc = start(a)
st = parse_qs(urlsplit(loc).query)["state"][0]
r = a.get(f"/connect/meta/callback?code={CODE}&state={st}", follow_redirects=True)
check("work. Try again in a minute" in html(r) and len(q("SELECT * FROM ad_connections")) == 1,
      "a token Meta says is for another app is refused")

# a SYSTEM_USER token (Business Integration) is kept as is and never expires
fake = FakeTransport()
adhttp.TRANSPORT = fake
meta_routes(fake, debug="meta/debug_token_system_user.json", pixels="meta/adspixels_one.json",
            me={"id": "10200000000000009", "name": "Glow Co system user"})
loc = start(b)
st = parse_qs(urlsplit(loc).query)["state"][0]
r = b.get(f"/connect/meta/callback?code={CODE}&state={st}")
check(len(fake.find("GET", "/oauth/access_token")) == 1, "a SYSTEM_USER token is not exchanged")
brow = q("SELECT * FROM ad_connections WHERE workspace_id=?", wsb["id"])[0]
check(brow["token_expires_at"] is None and adplatforms.open_token(brow) == SHORT, "...and is stored with no expiry")
r = b.post(r.headers["Location"], data={"csrf": csrf(b), "account": "222000222", "page": "500000002"})
brow = q("SELECT * FROM ad_connections WHERE workspace_id=?", wsb["id"])[0]
check(r.status_code == 302 and r.headers["Location"].endswith("/app/settings/ad-accounts") and brow["status"] == "active"
      and q("SELECT external_pixel_id FROM pixels WHERE workspace_id=?", wsb["id"])[0][0] == "700000003"
      and brow["instagram_actor_id"] is None, "one pixel is picked by itself; a Page without Instagram stays Facebook-only")
check(adplatforms.open_token(brow) == SHORT, "B's token opens in B's workspace")
try:
    secretsbox.open_box(brow["token_enc"], secretsbox.aad_for("ad_connections", wsa["id"]))
    check(False, "B's box opens as A's")
except secretsbox.BoxError:
    check(True, "B's token box does not open as workspace A's")

# ================================================================ 6. scoping and roles
print("scoping")
check(b.get(f"/app/settings/ad-accounts/{cid}/pick").status_code == 404, "another workspace's pick page is a 404")
check(b.get(f"/app/settings/ad-accounts/{cid}/pixel").status_code == 404, "another workspace's pixel page is a 404")
check(b.post(f"/app/settings/ad-accounts/{cid}/disconnect", data={"csrf": csrf(b)}).status_code == 404,
      "disconnecting another workspace's connection is a 404")
check(b.post("/connect/meta", data={"csrf": csrf(b), "reconnect": str(cid)}).status_code == 404,
      "reconnecting another workspace's connection is a 404")
check("Glow Co main" not in html(b.get("/app/settings/ad-accounts")), "B's Settings page doesn't list A's connection")
check(q("SELECT status FROM ad_connections WHERE id=?", cid)[0][0] == "active", "A's connection is untouched")
from werkzeug.security import generate_password_hash  # noqa: E402
with db.connect() as conn:
    mid = conn.execute("INSERT INTO users (email, name, pw_hash, workspace_id, role, created_at, email_verified_at) "
                       "VALUES (?,?,?,?,?,?,?)", ("member-a@example.com", "Mo", generate_password_hash("longpassword"),
                                                 wsa["id"], "member", db.now(), db.now())).lastrowid
    conn.commit()
m = client()
m.post("/login", data={"csrf": csrf(m), "email": "member-a@example.com", "password": "longpassword"})
page = html(m.get("/app/settings/ad-accounts"))
check("Glow Co main" in page and "Connect Meta</button>" not in page and "Disconnect</button>" not in page
      and "Only the workspace owner" in page, "a member sees the connections but no Connect or Disconnect")
check(m.post("/connect/meta", data={"csrf": csrf(m)}).status_code == 403, "a member can't start a connect")
check(m.post(f"/app/settings/ad-accounts/{cid}/disconnect", data={"csrf": csrf(m)}).status_code == 403,
      "a member can't disconnect")

# ================================================================ 7. reconnect after 190
print("reconnect")
fake = FakeTransport()
adhttp.TRANSPORT = fake
meta_routes(fake)
fake.on("GET", "/adspixels", "meta/error_190.json")
adplatforms.mark(db.connect(), cid, "expired", "Meta ended this access. Reconnect.")
page = html(a.get("/app/settings/ad-accounts"))
check("Needs reconnect" in page and "Reconnect</button>" in page, "an expired connection shows Needs reconnect and Reconnect")
loc = start(a, reconnect=str(cid))
st = parse_qs(urlsplit(loc).query)["state"][0]
r = a.get(f"/connect/meta/callback?code={CODE}&state={st}")
page = html(a.get(r.headers["Location"]))
check(re.search(r'value="111000111"\s+checked', page), "the pick page preselects the account being reconnected")
r = a.post(r.headers["Location"], data={"csrf": csrf(a), "account": "111000111", "page": "500000001"}, follow_redirects=True)
rows = q("SELECT * FROM ad_connections WHERE workspace_id=?", wsa["id"])
check(len(rows) == 1 and rows[0]["id"] == cid, "reconnecting updates the same connection (no duplicate, pending row gone)")
check(rows[0]["status"] == "expired" and "read its pixels; you can pick one later" in html(r),
      "a 190 while reading pixels marks it expired again and says so")
fake.on("GET", "/adspixels", "meta/adspixels_none.json")
loc = start(a, reconnect=str(cid))
st = parse_qs(urlsplit(loc).query)["state"][0]
r = a.get(f"/connect/meta/callback?code={CODE}&state={st}")
r = a.post(r.headers["Location"], data={"csrf": csrf(a), "account": "111000111", "page": "500000001"}, follow_redirects=True)
row = q("SELECT * FROM ad_connections WHERE id=?", cid)[0]
check(row["status"] == "active" and row["last_error"] is None and "no pixel yet" in html(r),
      "a good reconnect is active again; an account with no pixel says to make one in Events Manager")

# pending connections that were never picked go after an hour
with db.connect() as conn:
    conn.execute("INSERT INTO ad_connections (workspace_id, platform, external_account_id, token_enc, status, created_at, "
                 "updated_at) VALUES (?,?,?,?,?,?,?)", (wsa["id"], "meta", "pending:old", "x", "pending",
                                                         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    conn.commit()
a.get("/app/settings/ad-accounts")
check(not q("SELECT 1 FROM ad_connections WHERE external_account_id='pending:old'"), "an unpicked connection is dropped after an hour")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET token_expires_at='2026-01-01T00:00:00+00:00' WHERE id=?", (cid,))
    conn.commit()
a.get("/app/settings/ad-accounts")
check(q("SELECT status FROM ad_connections WHERE id=?", cid)[0][0] == "expired", "a token past its expiry reads as expired")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET token_expires_at=NULL, status='active' WHERE id=?", (cid,))
    conn.commit()

# ================================================================ 8. disconnect
print("disconnect")
# B's Meta login is the same person as a new connection in workspace C: revoking B must not cut C off
c3 = signup("owner-c@example.com")
wsc = ws_of("owner-c@example.com")
with db.connect() as conn:
    conn.execute("INSERT INTO ad_connections (workspace_id, platform, external_account_id, external_user_id, token_enc, "
                 "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (wsc["id"], "meta", "444", brow["external_user_id"], adplatforms.seal_token(wsc["id"], SHORT), "active",
                  db.now(), db.now()))
    conn.commit()
fake.calls.clear()
r = b.post(f"/app/settings/ad-accounts/{brow['id']}/disconnect", data={"csrf": csrf(b)}, follow_redirects=True)
check(not fake.find("DELETE") and "stays connected for your other" in html(r),
      "disconnect keeps Meta access when the same Meta login backs another connection")
brow = q("SELECT * FROM ad_connections WHERE id=?", brow["id"])[0]
check(brow["status"] == "revoked" and brow["token_enc"] is None and not q("SELECT 1 FROM pixels WHERE workspace_id=?", wsb["id"]),
      "...but the token and pixels are deleted here")
fake.calls.clear()
r = a.post(f"/app/settings/ad-accounts/{cid}/disconnect", data={"csrf": csrf(a)}, follow_redirects=True)
dl = fake.find("DELETE", "/me/permissions")
check(len(dl) == 1 and dl[0]["headers"]["Authorization"] == f"Bearer {LONG}" and "removed Campaignify&#39;s access" in html(r),
      "disconnect revokes our access at Meta (DELETE /me/permissions) when nobody else uses it")
row = q("SELECT * FROM ad_connections WHERE id=?", cid)[0]
check(row["status"] == "revoked" and row["token_enc"] is None and "Glow Co main" not in html(a.get("/app/settings/ad-accounts")),
      "the connection is gone from Settings and its token is deleted")
fake.on("DELETE", "/me/permissions", "meta/error_190.json")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET status='active', token_enc=?, external_user_id='other' WHERE id=?",
                 (adplatforms.seal_token(wsa["id"], LONG), cid))
    conn.commit()
r = a.post(f"/app/settings/ad-accounts/{cid}/disconnect", data={"csrf": csrf(a)}, follow_redirects=True)
check(q("SELECT status FROM ad_connections WHERE id=?", cid)[0][0] == "revoked" and "Business integrations" in html(r),
      "if Meta refuses the revoke, it's still disconnected here and the page says where to remove it on Facebook")

# ================================================================ 9. no secrets anywhere
print("secrets")
secrets_list = {"short token": SHORT, "long token": LONG, "app secret": APP_SECRET, "auth code": CODE,
                "PKCE verifier": VERIFIER, "TOKEN_KEY": KEY1, "leaky token": leak}
logs = "\n".join(CAP.lines)
check(len(CAP.lines) > 20 and "adhttp meta GET /v26.0/me/adaccounts -> 200" in logs, "the calls were logged (path and status)")
for what, val in secrets_list.items():
    check(val not in logs, f"no {what} in any log line")
with open(db.DB_PATH, "rb") as f:
    raw = f.read()
for p in (db.DB_PATH + "-wal",):
    if os.path.exists(p):
        raw += open(p, "rb").read()
for what in ("short token", "long token", "app secret"):
    check(secrets_list[what].encode() not in raw, f"no {what} in the database file")

print(f"\nall {passed} checks passed")

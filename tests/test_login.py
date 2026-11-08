"""Login hardening (login_security.py, roadmap M1.8) on a throwaway database:

  * failed logins: one message for every email; throttled per network + email, per network and per account
  * sign-up with an email that has an account: the "check your email" page and a note to that inbox
  * password reset: /forgot, a hashed single-use 60-minute token, the token off the address bar, throttles
  * nudge login links (/m): 24 hours, single use, spent only by the Log in button; RS_LOGIN_HARDENING=0
  * code throttles on Billing and at sign-up (BILL-6)

    python tests/test_login.py      (tests/test_app.py runs it too)
"""
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_login_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai", RS_PUBLIC_URL="https://campaignify.test")
for k in ("RS_LOGIN_HARDENING", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SIGNUP_CREDITS_ADS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import login_security  # noqa: E402
import mailer  # noqa: E402
import nudges  # noqa: E402

login_security.INLINE = True
passed = 0
sent = []


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": f"re_{len(sent)}"}


mailer.requests.post = lambda url, json=None, headers=None, timeout=0: (sent.append(json) or _Resp())

_ip = [0]


def client(ip=None):
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip or f"10.8.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def signup(email, pw="longpassword", c=None):
    c = c or client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": pw})
    return c, r


def login(email, pw, c=None):
    c = c or client()
    return c, c.post("/login", data={"csrf": csrf(c), "email": email, "password": pw})


def mails_to(email):
    return [m for m in sent if m["to"] == [email]]


_, r = signup("ana@example.com")
check(r.status_code == 302 and user("ana@example.com") is not None, "a plain sign-up still logs in and opens the app")

print("== failed logins: one message, three throttles")
_, r = login("ana@example.com", "wrong-password")
ana_msg = r.get_data(as_text=True)
_, r = login("nobody@example.com", "wrong-password")
check("password don" in ana_msg and "password don" in r.get_data(as_text=True)
      and re.search(r'class="flash">([^<]*)<', ana_msg).group(1) == re.search(r'class="flash">([^<]*)<', r.get_data(as_text=True)).group(1),
      "a known and an unknown email get the same words")
with db.connect() as conn:
    conn.execute("UPDATE users SET auth_provider='google' WHERE email='ana@example.com'")
    conn.commit()
_, r = login("ana@example.com", "wrong-password")
check("signs in with Google" not in r.get_data(as_text=True), "a Google account isn't singled out either")
with db.connect() as conn:
    conn.execute("UPDATE users SET auth_provider=NULL WHERE email='ana@example.com'")
    conn.commit()
signup("bo@example.com")
for i in range(login_security.FAILS_PER_ACCOUNT):
    login("bo@example.com", f"guess-{i}", client(ip=f"10.20.{i}.1"))
_, r = login("bo@example.com", "longpassword", client(ip="10.21.0.1"))
check(r.status_code == 429 and "reset your password" in r.get_data(as_text=True),
      f"{login_security.FAILS_PER_ACCOUNT} misses from different networks lock the account's password login, with a pointer to reset")
_, r = login("ana@example.com", "longpassword", client(ip="10.21.0.2"))
check(r.status_code == 302, "other accounts are not affected")
for i in range(login_security.FAILS_PER_ACCOUNT):
    login("ghost@example.com", f"guess-{i}", client(ip=f"10.22.{i}.1"))
_, r = login("ghost@example.com", "x", client(ip="10.21.0.3"))
check(r.status_code == 429, "an email with no account locks the same way (the lock says nothing about who exists)")
ipc = client(ip="10.23.0.1")
for i in range(login_security.FAILS_PER_IP):
    login(f"spray{i}@example.com", "x", ipc)
_, r = login("ana@example.com", "longpassword", ipc)
check(r.status_code == 429, f"{login_security.FAILS_PER_IP} misses from one network stop that network")
check(appmod.LOGIN_FAILS_PER_EMAIL == login_security.FAILS_PER_IP_EMAIL, "app.py keeps its old constant names")

print("== sign-up with an email that already has an account")
n_users = db.connect().execute("SELECT COUNT(*) FROM users").fetchone()[0]
n_mail = len(mails_to("ana@example.com"))
c, r = signup("ana@example.com", "anotherpassword")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "Check your email" in html and "already has an account" not in html
      and "data-signup-sent" in html, "the page says check your email, not that the account exists")
with c.session_transaction() as s:
    check("uid" not in s, "and logs nobody in")
check(db.connect().execute("SELECT COUNT(*) FROM users").fetchone()[0] == n_users, "no account is made")
m = mails_to("ana@example.com")[-1]
check(len(mails_to("ana@example.com")) == n_mail + 1 and m["subject"] == "You already have a Campaignify account"
      and m["from"] == '"Campaignify" <no-reply@mail.campaignify.ai>' and "https://campaignify.test/forgot" in m["text"]
      and "https://campaignify.test/login" in m["text"], "the inbox gets 'You already have an account' with log in and reset links")
_, r2 = signup("nobody-yet-2@example.com")
check(r2.status_code == 302, "a new email still signs up normally")
for _ in range(3):
    signup("ana@example.com", "anotherpassword")
check(len(mails_to("ana@example.com")) == n_mail + login_security.TAKEN_MAILS_PER_EMAIL_HOUR,
      f"at most {login_security.TAKEN_MAILS_PER_EMAIL_HOUR} of those emails an hour")
with db.connect() as conn:
    conn.execute("UPDATE users SET auth_provider='google' WHERE email='bo@example.com'")
    conn.commit()
signup("bo@example.com", "anotherpassword")
check("Continue with Google" in mails_to("bo@example.com")[-1]["text"], "a Google account is told, in the email, to use Google")
with db.connect() as conn:
    conn.execute("UPDATE users SET auth_provider=NULL WHERE email='bo@example.com'")
    conn.commit()
probe = client(ip="10.30.0.1")
codes = [signup(f"ana@example.com", "anotherpassword", probe)[1].status_code for _ in range(appmod.SIGNUPS_PER_HOUR)]
codes.append(signup("fresh-probe@example.com", "longpassword", probe)[1].status_code)
check(codes[-1] == 429, "trying taken emails counts against the sign-up limit (no address testing at scale)")

print("== password reset")
c = client()
r = c.get("/login")
check("/forgot" in r.get_data(as_text=True), "Log in links to Forgot your password?")
r = c.get("/forgot")
check(r.status_code == 200 and r.headers.get("X-Robots-Tag") == "noindex, nofollow", "/forgot opens (noindex)")
n = len(sent)
r = c.post("/forgot", data={"csrf": csrf(c), "email": "unknown@example.com"})
unknown_html = r.get_data(as_text=True)
check(r.status_code == 200 and "data-reset-sent" in unknown_html and len(sent) == n, "an unknown email: the same page, no email")
c = client()
r = c.post("/forgot", data={"csrf": csrf(c), "email": "Ana@Example.com "})
check(r.status_code == 200 and "data-reset-sent" in r.get_data(as_text=True), "a known email: the same page")
m = sent[-1]
link = re.search(r"https://campaignify\.test/reset\?t=([\w\-]+)", m["text"])
check(m["to"] == ["ana@example.com"] and m["subject"] == "Reset your Campaignify password" and link
      and m["from"] == '"Campaignify" <no-reply@mail.campaignify.ai>' and m.get("reply_to")
      and "60 minutes" in m["text"], "the reset email goes out from the account sender with a 60-minute link")
token = link.group(1)
with db.connect() as conn:
    rows = conn.execute("SELECT * FROM login_tokens WHERE kind='reset'").fetchall()
check(rows and all(token not in tuple(str(v) for v in row) for row in rows), "only a hash of the token is stored")
n = len(sent)
c.post("/forgot", data={"csrf": csrf(c), "email": "ana@example.com"})
check(len(sent) == n, "a second request inside a minute sends nothing")
r = c.get(f"/reset?t={token}")
check(r.status_code == 302 and r.headers["Location"].endswith("/reset"), "the link moves its token into the session")
r = c.get("/reset")
check(r.status_code == 200 and "ana@example.com" in r.get_data(as_text=True) and token not in r.get_data(as_text=True),
      "the new-password page has a clean address")
r = c.post("/reset", data={"csrf": csrf(c), "password": "short"})
check(r.status_code == 400, "a short password is refused")
r = c.post("/reset", data={"csrf": csrf(c), "password": "brand-new-pass"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app") and c.get("/app").status_code == 200,
      "a new password logs you in")
_, r = login("ana@example.com", "longpassword")
check(r.status_code == 200, "the old password stops working")
_, r = login("ana@example.com", "brand-new-pass")
check(r.status_code == 302, "the new one works")
c2 = client()
r = c2.get(f"/reset?t={token}", follow_redirects=True)
check("expired or was already used" in r.get_data(as_text=True), "the link works once")
# two links: using one ends the other
c3 = client()
c3.post("/forgot", data={"csrf": csrf(c3), "email": "bo@example.com"})
t1 = re.search(r"/reset\?t=([\w\-]+)", sent[-1]["text"]).group(1)
with db.connect() as conn:
    conn.execute("UPDATE throttle SET at=at-120 WHERE key='reset:bo@example.com'")
    conn.commit()
c3.post("/forgot", data={"csrf": csrf(c3), "email": "bo@example.com"})
t2 = re.search(r"/reset\?t=([\w\-]+)", sent[-1]["text"]).group(1)
check(t1 != t2, "a second request gives a new link")
r = login("bo@example.com", "longpassword", client(ip="10.21.0.9"))[1]
check(r.status_code == 429, "(Bo's password login is still locked from the guesses above)")
c3.get(f"/reset?t={t2}")
r = c3.post("/reset", data={"csrf": csrf(c3), "password": "bo-new-password"})
check(r.status_code == 302, "Bo resets with the newer link")
check("expired" in client().get(f"/reset?t={t1}", follow_redirects=True).get_data(as_text=True),
      "and the older link stops working")
_, r = login("bo@example.com", "bo-new-password", client(ip="10.21.0.10"))
check(r.status_code == 302, "a reset lifts the account's login lock")
# expiry and a changed email
with db.connect() as conn:
    u = user("ana@example.com")
    old = login_security.issue_reset(conn, u)
    conn.execute("UPDATE login_tokens SET expires_at=? WHERE token_hash=?", (int(time.time()) - 1, login_security._hash(old)))
    moved = login_security.issue_reset(conn, u)
    conn.execute("UPDATE login_tokens SET email='before@example.com' WHERE token_hash=?", (login_security._hash(moved),))
    conn.commit()
check("expired" in client().get(f"/reset?t={old}", follow_redirects=True).get_data(as_text=True), "an expired link is refused")
check("expired" in client().get(f"/reset?t={moved}", follow_redirects=True).get_data(as_text=True),
      "a link for an email that has since changed is refused")
check(client().get("/reset", follow_redirects=True).request.path == "/forgot", "/reset without a link goes to /forgot")
# an unconfirmed account is confirmed by a reset (the link reached the inbox)
signup("cy@example.com")
with db.connect() as conn:
    check(not user("cy@example.com")["email_verified_at"], "(Cy hasn't confirmed yet)")
    tok = login_security.issue_reset(conn, user("cy@example.com"))
c = client()
c.get(f"/reset?t={tok}")
c.post("/reset", data={"csrf": csrf(c), "password": "cy-password-1"})
check(user("cy@example.com")["email_verified_at"], "a reset confirms the email")
ipc = client(ip="10.40.0.1")
codes = [ipc.post("/forgot", data={"csrf": csrf(ipc), "email": f"r{i}@example.com"}).status_code
         for i in range(login_security.RESETS_PER_IP_HOUR + 1)]
check(codes[-1] == 429 and set(codes[:-1]) == {200}, f"{login_security.RESETS_PER_IP_HOUR} reset requests an hour per network")

print("== nudge login links: 24 hours, once, by button")
with appmod.app.test_request_context():
    u = user("ana@example.com")
    tok = nudges.magic_token(appmod.app.secret_key, u)
subject, text, html = nudges._copy(1, False, "https://x/m?t=1")
check("works for 24 hours" in text and "logs you in once" in text and "72" not in text, "the email says 24 hours, once")
m = client()
r = m.get(f"/m?t={tok}")
check(r.status_code == 302 and r.headers["Location"].endswith("/m"), "the link moves its token off the address bar")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM login_tokens WHERE kind='magic'").fetchone()[0] == 0,
          "opening it spends nothing (a scanner's fetch is harmless)")
r = m.get("/m")
check(r.status_code == 200 and "ana@example.com" in r.get_data(as_text=True) and tok not in r.get_data(as_text=True),
      "it shows a Log in button")
r = m.post("/m", data={"csrf": csrf(m)})
check(r.status_code == 302 and r.headers["Location"].endswith("/app") and m.get("/app").status_code == 200, "the button logs in")
again = client()
r = again.get(f"/m?t={tok}", follow_redirects=True)
check(r.request.path == "/login" and "already used" in r.get_data(as_text=True), "the same link doesn't work twice")
import itsdangerous.timed as _timed  # noqa: E402


class _Past:
    @staticmethod
    def time():
        return time.time() - 25 * 3600


_real = _timed.time
_timed.time = _Past
try:
    with appmod.app.test_request_context():
        stale = nudges.magic_token(appmod.app.secret_key, user("bo@example.com"))
finally:
    _timed.time = _real
r = client().get(f"/m?t={stale}", follow_redirects=True)
check(r.request.path == "/login", "a 25-hour-old link is refused")
check(client().get("/m?t=junk", follow_redirects=True).request.path == "/login", "a bad link goes to Log in")
os.environ["RS_LOGIN_HARDENING"] = "0"
try:
    r = client().get(f"/m?t={stale}")
    check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "RS_LOGIN_HARDENING=0: 72-hour links again")
    r = client().get(f"/m?t={stale}")
    check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "and reusable, as before")
    check("72 hours" in nudges._copy(1, False, "https://x/m?t=1")[1], "with the old email copy")
    _, r = login("ana@example.com", "wrong")
    check("password don" in r.get_data(as_text=True), "the old login message")
    _, r = signup("ana@example.com", "anotherpassword")
    check("already has an account" in r.get_data(as_text=True), "and the old sign-up message")
finally:
    os.environ.pop("RS_LOGIN_HARDENING")

print("== code throttles (BILL-6)")
o, _ = signup("owner@example.com")
for i in range(login_security.CODES_PER_HOUR):
    o.post("/app/billing/code", data={"csrf": csrf(o), "code": f"CFY-AAAA-AAA{i}"})
r = o.post("/app/billing/code", data={"csrf": csrf(o), "code": "CFY-AAAA-AAAB"}, follow_redirects=True)
check(login_security.CODES_MSG in r.get_data(as_text=True), f"{login_security.CODES_PER_HOUR} codes an hour per workspace on Billing")
r = o.get("/app/billing?code=TRIAL30")
check(login_security.CODES_MSG in r.get_data(as_text=True), "billing ?code= links share the limit (no Stripe lookup past it)")
sc = client(ip="10.50.0.1")
codes = []
for i in range(login_security.CODES_PER_HOUR + 1):
    codes.append(sc.post("/signup", data={"csrf": csrf(sc), "email": f"code{i}@example.com", "password": "longpassword",
                                          "comp_code": "CFY-ZZZZ-ZZZZ"}).status_code)
check(codes[-1] == 429 and 429 not in codes[:-1], f"{login_security.CODES_PER_HOUR} sign-up codes an hour per network")
r = sc.post("/signup", data={"csrf": csrf(sc), "email": "nocode@example.com", "password": "longpassword"})
check(r.status_code == 302, "a sign-up without a code isn't held up")

print(f"\nall {passed} login checks passed ({TMP})")

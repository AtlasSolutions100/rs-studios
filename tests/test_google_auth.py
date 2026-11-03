"""Continue with Google (google_auth.py) on a throwaway database, Google's HTTP faked.

    python tests/test_google_auth.py

  * unconfigured: no button, /auth/google is a 404
  * configured: the button on Log in and Start free, the redirect to Google carries state and nonce
  * the callback: bad state, a new email (an account exactly like the sign-up form makes), an existing
    password account (logged in, nothing new), an existing unconfirmed account (now confirmed)
  * a password login against a Google account says to use the Google button
"""
import os
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_google_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_PUBLIC_URL="https://campaignify.test")
os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET", None)
os.environ.pop("GOOGLE_OAUTH_CLIENT_ID", None)
os.environ.pop("SIGNUP_CREDITS_ADS", None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import google_auth  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()


def count(sql):
    with db.connect() as conn:
        return conn.execute(sql).fetchone()[0]


def start(c, **args):
    """Open /auth/google and return the query Google would receive."""
    r = c.get("/auth/google", query_string=args)
    check(r.status_code == 302 and r.headers["Location"].startswith(google_auth.AUTH_URL + "?"),
          "/auth/google redirects to accounts.google.com")
    return {k: v[0] for k, v in parse_qs(urlsplit(r.headers["Location"]).query).items()}


# What Google will say for the next callback (claims of the id_token) and what it was asked.
GOOGLE = {"claims": {}, "codes": [], "tokens": []}


def fake_exchange(code):
    GOOGLE["codes"].append(code)
    return {"id_token": "idt-" + code, "access_token": "at-" + code, "token_type": "Bearer"}


def fake_tokeninfo(id_token):
    GOOGLE["tokens"].append(id_token)
    return dict(GOOGLE["claims"])


google_auth.exchange_code = fake_exchange
google_auth.verify_id_token = fake_tokeninfo


def google_says(email, name="Pat Example", verified=True, nonce=None, **extra):
    claims = {"iss": "https://accounts.google.com", "aud": google_auth.client_id(), "sub": "sub-" + email,
              "email": email, "email_verified": "true" if verified else "false", "name": name}
    if nonce is not None:
        claims["nonce"] = nonce
    claims.update(extra)
    GOOGLE["claims"] = claims


def sign_in(c, email, **kw):
    """The whole round trip: start, Google answers, the callback."""
    q = start(c, **{k: v for k, v in kw.items() if k in ("next", "invite")})
    google_says(email, nonce=q["nonce"], **{k: v for k, v in kw.items() if k not in ("next", "invite")})
    return c.get("/auth/google/callback", query_string={"state": q["state"], "code": "code-" + email})


print("== not configured")
check(not google_auth.configured(), "without GOOGLE_OAUTH_CLIENT_SECRET, Google sign-in is off")
c = client()
check("Continue with Google" not in c.get("/login").get_data(as_text=True), "no Google button on Log in")
check("Continue with Google" not in c.get("/signup").get_data(as_text=True), "no Google button on Start free")
check(c.get("/auth/google").status_code == 404, "/auth/google is a 404")
check(c.get("/auth/google/callback?state=x&code=y").status_code == 404, "and so is the callback")

print("== configured")
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-only-not-a-real-secret"
check(google_auth.configured() and google_auth.client_id() == google_auth.DEFAULT_CLIENT_ID,
      "with the secret set it is on, with the default client id")
check(google_auth.redirect_uri() == "https://campaignify.test/auth/google/callback",
      "the redirect URI comes from RS_PUBLIC_URL, not the request host")
c = client()
html = c.get("/login?next=/app/library").get_data(as_text=True)
check("Continue with Google" in html and 'href="/auth/google?next=/app/library"' in html and "<svg" in html,
      "Log in shows the button, carrying next, with an inline G")
html = c.get("/signup?invite=abc123").get_data(as_text=True)
check("Continue with Google" in html and 'href="/auth/google?invite=abc123"' in html,
      "Start free shows the button, carrying the invite code")
html = c.get("/signup").get_data(as_text=True)
check('href="/auth/google"' in html and "What happens next" in html and 'name="password"' in html,
      "the plain sign-up page keeps the What happens next panel and the email form")
q = start(c, next="/app/library")
check(q["client_id"] == google_auth.DEFAULT_CLIENT_ID and q["redirect_uri"] == google_auth.redirect_uri()
      and q["response_type"] == "code" and q["scope"] == "openid email profile" and q["prompt"] == "select_account"
      and q["access_type"] == "online" and len(q["state"]) >= 24 and len(q["nonce"]) >= 24,
      "the redirect to Google carries the client id, our redirect URI, the scopes, a state and a nonce")
with c.session_transaction() as s:
    check(s["g_state"] == q["state"] and s["g_nonce"] == q["nonce"] and s["g_next"] == "/app/library",
          "state, nonce and next are kept in the session")
r = c.get("/auth/google?next=https://evil.example/")
with c.session_transaction() as s:
    check("g_next" not in s, "a next on another host is dropped")
r = c.get("/auth/google?next=//evil.example/")
with c.session_transaction() as s:
    check("g_next" not in s, "and so is a protocol-relative one")

print("== the callback: bad state")
c = client()
q = start(c)
r = c.get("/auth/google/callback", query_string={"state": "not-the-state", "code": "code-x"})
check(r.status_code == 302 and r.headers["Location"].endswith("/login"), "a wrong state goes back to Log in")
html = c.get("/login").get_data(as_text=True)
check("expired" in html, "with a short message")
with c.session_transaction() as s:
    check("g_state" not in s and "uid" not in s, "the state is used up and nobody is logged in")
check(GOOGLE["codes"] == [], "Google was never asked for a token")
c = client()
q = start(c)
r = c.get("/auth/google/callback", query_string={"state": q["state"], "error": "access_denied"})
check(r.status_code == 302 and r.headers["Location"].endswith("/login")
      and "cancelled" in c.get("/login").get_data(as_text=True), "Google's own error (the person pressed Cancel) goes back to Log in")
c = client()
r = c.get("/auth/google/callback", query_string={"state": "x", "code": "y"})
check(r.headers["Location"].endswith("/login"), "a callback with no round trip started goes back to Log in")

print("== the callback: a new email")
users0, ws0 = count("SELECT COUNT(*) FROM users"), count("SELECT COUNT(*) FROM workspaces")
c = client()
r = sign_in(c, "pat@example.com")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "a new email lands on Home (/app)")
u = user("pat@example.com")
check(u is not None and count("SELECT COUNT(*) FROM users") == users0 + 1 and count("SELECT COUNT(*) FROM workspaces") == ws0 + 1,
      "one user and one workspace were made")
check(u["auth_provider"] == "google" and u["email_verified_at"] and u["verify_nonce"] is None and u["role"] == "owner"
      and u["name"] == "Pat Example" and u["is_admin"] == 0, "auth_provider google, email confirmed, no verify nonce, owner of it")
with db.connect() as conn:
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()
    ev = conn.execute("SELECT name, detail FROM site_events WHERE workspace_id=? AND name='signup'", (ws["id"],)).fetchone()
check(ws["name"] == "Pat Example's studio" and ws["credits"] == catalog.starter_credits() == 44,
      "the workspace has the 44 starter credits")
check(ev is not None and ev["detail"] == "starter", "the funnel's sign-up step is recorded as a starter sign-up")
with c.session_transaction() as s:
    check(s.get("uid") == u["id"] and s.get("csrf"), "the session is logged in with a form token")
check(c.get("/app").status_code == 200, "Home opens")
check(GOOGLE["codes"][-1] == "code-pat@example.com" and GOOGLE["tokens"][-1] == "idt-code-pat@example.com",
      "the code was exchanged and the id_token checked with Google")
r = c.get("/auth/google")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "logged in, /auth/google just opens Home")

print("== the callback: an existing password account")
c = client()
c.get("/signup")
r = c.post("/signup", data={"csrf": csrf(c), "email": "sam@example.com", "password": "longenough1"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "the email sign-up form still makes an account")
sam = user("sam@example.com")
check(sam["auth_provider"] is None and sam["email_verified_at"] is None and sam["verify_nonce"]
      and count("SELECT COUNT(*) FROM workspaces") == ws0 + 2, "a form account: no provider, confirms by email, its own workspace")
c2 = client()
r = sign_in(c2, "sam@example.com", name="Sam Someone")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "Google with that email logs in")
with c2.session_transaction() as s:
    check(s.get("uid") == sam["id"], "as that account")
sam2 = user("sam@example.com")
check(count("SELECT COUNT(*) FROM users") == users0 + 2 and count("SELECT COUNT(*) FROM workspaces") == ws0 + 2
      and sam2["workspace_id"] == sam["workspace_id"] and sam2["auth_provider"] is None and sam2["name"] == sam["name"],
      "no new user or workspace; the account is unchanged apart from the confirmation")
check(sam2["email_verified_at"] and sam2["verify_nonce"] is None,
      "an unconfirmed account is now confirmed (Google verified the address)")
with db.connect() as conn:
    ev = conn.execute("SELECT 1 FROM site_events WHERE workspace_id=? AND name='verify_completed'",
                      (sam["workspace_id"],)).fetchone()
check(ev is not None, "and verify_completed is recorded")
c3 = client()
r = sign_in(c3, "sam@example.com", next="/app/library")
check(r.headers["Location"].endswith("/app/library"), "next is followed for an existing account")

print("== the callback: refusals")
c = client()
r = sign_in(c, "nobody@example.com", verified=False)
check(r.headers["Location"].endswith("/login") and user("nobody@example.com") is None
      and "Google sign-in did" in c.get("/login").get_data(as_text=True), "an email Google has not verified makes no account")
c = client()
r = sign_in(c, "other@example.com", aud="someone-else.apps.googleusercontent.com")
check(r.headers["Location"].endswith("/login") and user("other@example.com") is None, "an id_token for another client is refused")
c = client()
r = sign_in(c, "iss@example.com", iss="https://evil.example")
check(user("iss@example.com") is None, "and so is one from another issuer")
c = client()
q = start(c)
google_says("nonce@example.com", nonce="a-different-nonce")
r = c.get("/auth/google/callback", query_string={"state": q["state"], "code": "code-nonce"})
check(user("nonce@example.com") is None and r.headers["Location"].endswith("/login"), "a nonce mismatch is refused")


def boom(code):
    raise ConnectionError("google down")


keep = google_auth.exchange_code
google_auth.exchange_code = boom
c = client()
q = start(c)
r = c.get("/auth/google/callback", query_string={"state": q["state"], "code": "code-down"})
check(r.headers["Location"].endswith("/login") and "Google sign-in did" in c.get("/login").get_data(as_text=True),
      "Google unreachable: a plain message, back to Log in")
google_auth.exchange_code = keep

print("== password login against a Google account")
c = client()
c.get("/login")
r = c.post("/login", data={"csrf": csrf(c), "email": "pat@example.com", "password": "whatever-this-is"})
html = r.get_data(as_text=True)
check(r.status_code == 200 and "This account signs in with Google. Use the Google button." in html,
      "a password login on a Google account says to use the Google button")
r = c.post("/login", data={"csrf": csrf(c), "email": "sam@example.com", "password": "wrong-password"})
check("password don" in r.get_data(as_text=True) and "signs in with Google" not in r.get_data(as_text=True),
      "a password account keeps the generic message")
r = c.post("/login", data={"csrf": csrf(c), "email": "ghost@example.com", "password": "wrong-password"})
check("password don" in r.get_data(as_text=True), "and so does an unknown email")
r = c.post("/login", data={"csrf": csrf(c), "email": "sam@example.com", "password": "longenough1"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "Sam's password still works after signing in with Google")

print("== invites and the signup rate limit")
with db.connect() as conn:
    ws_id = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)", ("Team", 0, db.now())).lastrowid
    conn.execute("INSERT INTO invites (code, workspace_id, created_by, created_at) VALUES (?,?,?,?)",
                 ("inv-google", ws_id, None, db.now()))
    conn.commit()
c = client()
r = sign_in(c, "teammate@example.com", invite="inv-google")
t = user("teammate@example.com")
check(r.headers["Location"].endswith("/app") and t["workspace_id"] == ws_id and t["role"] == "member",
      "an invite link joins the team through Google too")
with db.connect() as conn:
    inv = conn.execute("SELECT used_by FROM invites WHERE code='inv-google'").fetchone()
check(inv["used_by"] == t["id"], "and uses up the invite")
c = client()
r = sign_in(c, "late@example.com", invite="inv-google")
check(r.status_code == 302 and "/signup" in r.headers["Location"] and user("late@example.com") is None,
      "a used invite makes no account and goes back to Start free")
c = client()
ip = c.environ_base["REMOTE_ADDR"]
for _ in range(appmod.SIGNUPS_PER_HOUR):
    db.hit(f"signup:{ip}")
r = sign_in(c, "sixth@example.com")
check(r.headers["Location"].endswith("/login") and user("sixth@example.com") is None
      and "Too many" in c.get("/login").get_data(as_text=True), "the per-network sign-up limit applies to Google too")

print(f"\nall {passed} Google sign-in checks passed ({TMP})")

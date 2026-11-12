"""Everyone confirms their email (verify.everyone, confirm_step.py), on a throwaway database with email and Stripe
faked. Nothing leaves the machine.

  * the rule: on while plans only is on, RS_EMAIL_VERIFY=all / video / 0 say otherwise
  * a new sign-up is sent to the step before the plan page; no checkout starts and nothing spends until it confirms
  * the code and the emailed link both confirm; a wrong code says how many tries are left
  * the step's address carries no query, so the sign-up conversion still fires on it (the first page after sign-up)
  * the library, Billing and logging out stay open; scripts get JSON that opens the code box
  * a member already on a plan is asked too, and nothing they paid for is locked away
  * "Send a new code" and its limits; a dead code is replaced when the page opens
  * "Wrong address?": the password is asked, the code goes to the new address, the account moves to it only when
    that inbox confirms, and an address that already has an account is told about it while the asker is told nothing
  * site admins are never asked and can look at the step; the free-account rule still stands alone with =video
  * the new look: the questions and the brief come first, then the step, then the plan
  * the emails say nothing about free credits while plans only is on; the reminder's link logs in and confirms

    python tests/test_confirm_step.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_confirm_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  STRIPE_SECRET_KEY="sk_test_cf", STRIPE_WEBHOOK_SECRET="whsec_cf", PLANS_ONLY="1", IMPORT_ENRICH="0",
                  META_PIXEL_ID="1234567890")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "RS_EMAIL_VERIFY", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "AD_LAUNCH", "RS_OWNER_INVITES", "RS_FIRST_OWNER_EMAIL", "SIGNUP_CREDITS_ADS",
          "RS_STARTER_CREDITS", "STRIPE_SETUP", "PLAN_SALE", "FIRST_RUN_NUDGES"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import confirm_step  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402
import login_security  # noqa: E402
import mailer  # noqa: E402
import nudges  # noqa: E402
import pixels  # noqa: E402
import plan_gate  # noqa: E402
import verify  # noqa: E402
from flask import g, request  # noqa: E402

passed = 0
SECRET = appmod.app.secret_key
PW = "longpassword1"


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


# ---------------------------------------------------------------- fakes: email and Stripe
sent, posts = [], []
mailer.configured = lambda: True
mailer.send = lambda **kw: sent.append(kw) or f"em_{len(sent)}"
login_security.INLINE = True


def fake_post(path, form, idempotency_key=None):
    posts.append(path)
    return {"id": f"cs_test_{len(posts)}", "url": f"https://checkout.stripe.test/c/{len(posts)}"}


billing._post = fake_post
billing._get = lambda path, params=None: {"object": "list", "data": []}
appmod._maybe_reconcile = lambda ws_id, force=False: 0

# ---------------------------------------------------------------- helpers
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.21.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": PW})
    assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:300])
    return c


def user_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def user_id(uid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def run(sql, *a):
    with db.connect() as conn:
        conn.execute(sql, a)
        conn.commit()


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.pop("_flashes", [])]


def text(r):
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.get_data(as_text=True)))).strip()


def mail_to(addr, kind=None):
    return [m for m in sent if m["to"] == addr and (kind is None or m["tags"]["kind"] == kind)]


def code_in(m):
    return re.search(r"code: (\d{6})", m["subject"]).group(1)


def link_in(m):
    return re.search(r"https://campaignify\.test(/verify\?t=\S+)", m["text"]).group(1)


def post(c, **data):
    return c.post("/app/confirm", data=dict(data, csrf=csrf(c)))


def forget_sends(uid):
    """Let this account ask for another email now (the once-a-minute limit)."""
    run("DELETE FROM throttle WHERE key=?", f"verify:{uid}")


def wrong_code(right):
    return "000000" if right != "000000" else "111111"


def plan(email, code="growth"):
    run("UPDATE workspaces SET plan=?, plan_status='active' WHERE id=?", code, user_of(email)["workspace_id"])


# ================================================================ the rule
print("the rule")
check(plan_gate.on() and verify.everyone(), "plans only is on, so everyone confirms")
os.environ["RS_EMAIL_VERIFY"] = "video"
check(verify.required() and not verify.everyone(), "RS_EMAIL_VERIFY=video keeps the free-account rule alone")
os.environ["RS_EMAIL_VERIFY"] = "0"
check(not verify.required() and not verify.everyone(), "RS_EMAIL_VERIFY=0 asks nothing")
os.environ.pop("PLANS_ONLY")
os.environ["RS_EMAIL_VERIFY"] = "1"
check(not verify.everyone(), "with the free tier on, the free-account rule is the rule")
os.environ["RS_EMAIL_VERIFY"] = "all"
check(verify.everyone(), "RS_EMAIL_VERIFY=all asks everyone with the free tier on too")
os.environ.pop("RS_EMAIL_VERIFY")
os.environ["PLANS_ONLY"] = "1"
check(verify.everyone(), "back to the default for the rest of this file")

# ================================================================ a new sign-up, the current look
print("a new sign-up")
A = "ana@example.com"
a = signup(A)
ua = user_of(A)
m = mail_to(A, "verify_email")
check(len(m) == 1 and not ua["email_verified_at"], "sign-up sends one email with the code")
check(verify.WHY_CONFIRM in m[0]["text"] and "credits" not in m[0]["text"] and "image ad" not in m[0]["text"],
      "that email describes no free credits: there are none with plans only")
r = a.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/plan"), "home sends a sign-up to the plan step")
r = a.get("/app/plan")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/confirm"),
      "the plan step sends an unconfirmed sign-up to Confirm your email")
with appmod.app.test_request_context(r.headers["Location"]):
    check(pixels.page_ok(request), "the step's address carries no query, so the ad pixels may load on it")
r = a.get("/app/confirm")
body = r.get_data(as_text=True)
check(r.status_code == 200 and "data-confirm-step" in body and A in body, "the step shows the address the code went to")
check("fbevents.js" in body and '"name": "SignUp"' in body and f"signup-{ua['id']}" in body,
      "the sign-up conversion fires here: the first page a sign-up sees in the current look")
check('"name": "SignUp"' not in a.get("/app/confirm").get_data(as_text=True), "once")
check(len(mail_to(A)) == 1, "opening the step sends nothing while the sign-up's code still works")
check("data-confirm-looking" not in body, "a member is not told they are only looking")

r = a.post("/app/billing/subscribe", data={"csrf": csrf(a), "plan": "starter"})
check(r.status_code == 302 and "/app/confirm" in r.headers["Location"] and not posts,
      "a plan cannot be started before the email is confirmed: no checkout is opened at Stripe")
r = a.post("/app/billing/subscribe", data={"csrf": csrf(a), "plan": "starter"}, headers={"Accept": "application/json"})
j = r.get_json()
check(r.status_code == 403 and j["verify"] is True and j["reason"] == "confirm" and j["url"] == "/app/confirm"
      and A in j["error"], "a script is answered with JSON that opens the code box")
check(a.post("/app/billing/code", data={"csrf": csrf(a), "code": "FREE-MONTH"}).headers["Location"].endswith("/app/confirm"),
      "a free-month code waits for the confirmation too")
check(a.get("/app/library").status_code == 200, "the library stays open")
check(a.get("/app/billing").status_code == 200, "Billing stays open")
with appmod.app.test_request_context("/app"):
    g.user, g.ws = ua, db.connect().execute("SELECT * FROM workspaces WHERE id=?", (ua["workspace_id"],)).fetchone()
    try:
        core.hold_check()
        raised = None
    except Exception as e:  # noqa: BLE001
        raised = e
    check(isinstance(raised, verify.ConfirmRequired), "nothing spends for an unconfirmed member: the email comes before the plan")
    with db.connect() as conn:
        check(verify.blocked(conn, g.ws, ua) and verify.needs(conn, g.ws, ua, "ad_image", 10) == "confirm",
              "every kind of ad waits for the confirmation, image ads too")

code = code_in(m[0])
r = post(a, do="check", code=wrong_code(code))
check(r.status_code == 400 and "That code isn't right. 4 tries left." in text(r) and 'aria-invalid="true"' in r.get_data(as_text=True),
      "a wrong code says how many tries are left")
r = post(a, do="check", code="12")
check(r.status_code == 400 and "Enter the 6-digit code" in text(r), "half a code is asked for again")
check(not user_of(A)["email_verified_at"], "neither confirmed anything")
r = post(a, do="check", code=f"{code[:3]} {code[3:]}")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/plan") and user_of(A)["email_verified_at"],
      "the right code confirms the email (a space in it is fine) and goes on to where they were headed, the plan step")
check(flashes(a) == ["Email confirmed."], "and says so, with nothing about video")
r = a.get("/app/plan")
check(r.status_code == 200 and "data-plan-step" in r.get_data(as_text=True), "the plan step opens now")
r = a.post("/app/billing/subscribe", data={"csrf": csrf(a), "plan": "starter"})
check(r.status_code == 303 and "checkout.stripe.test" in r.headers["Location"] and posts == ["/checkout/sessions"],
      "and a plan checkout starts")
r = a.get("/app/confirm")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "a confirmed member is sent on from the step")
check(a.get("/app/confirm?next=//evil.example").headers["Location"].endswith("/app")
      and confirm_step._next("//evil.example") == "" and confirm_step._next("https://evil.example/app") == ""
      and confirm_step._next("/app/confirm") == "" and confirm_step._next("/app/library?q=1") == "/app/library?q=1",
      "the step only ever goes on to a page of ours, and never takes that from the address")
with db.connect() as conn:
    check(conn.execute("SELECT 1 FROM site_events WHERE name='verify_completed' AND workspace_id=?",
                       (ua["workspace_id"],)).fetchone() is not None, "the funnel records the confirmation")

# ================================================================ the emailed link
print("the emailed link")
B = "ben@example.com"
b = signup(B)
r = b.get(link_in(mail_to(B)[0]))
check(r.status_code == 302 and user_of(B)["email_verified_at"], "the link in the email confirms too")
check(flashes(b) == ["Email confirmed."], "and says so")
check(b.get("/app/plan").status_code == 200, "then the plan step opens")

# ================================================================ send a new code
print("send a new code")
C = "cara@example.com"
c = signup(C)
uc = user_of(C)
first = code_in(mail_to(C)[0])
r = post(c, do="resend")
check(r.status_code == 302 and len(mail_to(C)) == 1 and "We just sent one" in flashes(c)[0],
      "a second email within a minute is not sent, and the page says why")
forget_sends(uc["id"])
r = post(c, do="resend")
check(len(mail_to(C)) == 2 and f"Sent a new code to {C}" in flashes(c)[0], "after that a new code goes out")
second = code_in(mail_to(C)[1])
if first != second:
    r = post(c, do="check", code=first)
    check(r.status_code == 400 and not user_of(C)["email_verified_at"], "the older code stops working")
run("UPDATE users SET verify_code_exp=? WHERE id=?", int(time.time()) - 5, uc["id"])
forget_sends(uc["id"])
r = c.get("/app/confirm")
check(r.status_code == 200 and len(mail_to(C)) == 3, "a code that has run out is replaced when the step opens")
r = c.get("/app/confirm")
check(len(mail_to(C)) == 3, "and opening it again sends nothing more")
r = post(c, do="check", code=second)
check(r.status_code == 400 and "isn't right" in text(r), "the replaced code is refused")
for _ in range(4):
    r = post(c, do="check", code=wrong_code(code_in(mail_to(C)[2])))
check("Too many wrong tries. Press Send a new code for a new one." in text(r), "five wrong tries lock that code, and the page names its own button")
r = post(c, do="check", code=code_in(mail_to(C)[2]))
check(r.status_code == 400 and not user_of(C)["email_verified_at"], "a locked code does not confirm, even typed right")

# ================================================================ wrong address
print("wrong address")
D, D2 = "dana@example.net", "dana@example.com"
d = signup(D)
ud = user_of(D)
old_mail = mail_to(D)[0]
r = post(d, do="change", email=D2, password="not-the-password")
check(r.status_code == 400 and "That password isn't right." in text(r) and "<details class=\"cem-change\" open" in r.get_data(as_text=True)
      and user_id(ud["id"])["pending_email"] is None, "a wrong password changes nothing, and the form stays open with the reason")
r = post(d, do="change", email="not an address", password=PW)
check(r.status_code == 400 and "Enter the email address you want to use." in text(r), "something that is no address is refused")
forget_sends(ud["id"])
r = post(d, do="change", email=D2, password=PW)
now = user_id(ud["id"])
check(r.status_code == 302 and now["pending_email"] == D2 and now["email"] == D and not now["email_verified_at"],
      "the right password keeps the new address aside: the account's email has not changed yet")
check(flashes(d) == [f"We sent a code to {D2}. It can take a minute; check spam too."], "the page says where the code went")
new_mail = mail_to(D2, "verify_email")
check(len(new_mail) == 1 and len(mail_to(D)) == 1, "the code went to the new address only")
r = d.get("/app/confirm")
check(D2 in text(r) and f"You log in with {D} until the new address is confirmed." in text(r),
      "the step shows the new address and says which one still logs in")
r = post(d, do="check", code=code_in(old_mail))
check(r.status_code == 400 and not user_id(ud["id"])["email_verified_at"], "the code sent to the old address no longer works")
r = d.get(link_in(old_mail))
check(not user_id(ud["id"])["email_verified_at"], "nor does the old address's link")
flashes(d)
r = post(d, do="check", code=code_in(new_mail[0]))
now = user_id(ud["id"])
check(r.status_code == 302 and now["email"] == D2 and now["email_verified_at"] and now["pending_email"] is None
      and now["email_norm"] == db.norm_email(D2), "the new address's code confirms it, and the account moves to it")
x = client()
check(x.post("/login", data={"csrf": csrf(x), "email": D2, "password": PW}).status_code == 302, "the new address logs in")
x = client()
check(x.post("/login", data={"csrf": csrf(x), "email": D, "password": PW}).status_code == 200, "the old one no longer does")

E = "eli@example.com"
e = signup(E)
ue = user_of(E)
forget_sends(ue["id"])
before = len(sent)
r = post(e, do="change", email=A, password=PW)       # ana@ already has an account
now = user_id(ue["id"])
check(r.status_code == 302 and flashes(e) == [f"We sent a code to {A}. It can take a minute; check spam too."]
      and now["pending_email"] == A and now["email"] == E,
      "an address that already has an account gets the same answer as a free one")
new = sent[before:]
check(len(new) == 1 and new[0]["to"] == A and new[0]["tags"]["kind"] == "account_exists",
      "that inbox is told someone tried its address, and no code is sent anywhere")
with db.connect() as conn:
    minted = verify.new_code(conn, SECRET, now)       # even with a working code for it
    conn.commit()
r = post(e, do="check", code=minted)
now = user_id(ue["id"])
check(r.status_code == 400 and "can't be used for this account" in text(r) and now["email"] == E and not now["email_verified_at"],
      "the account can never take an address that belongs to another one")
check(user_of(A)["id"] == ua["id"], "and the other account is untouched")
forget_sends(ue["id"])
r = post(e, do="change", email=E, password=PW)
check(user_id(ue["id"])["pending_email"] is None and len(mail_to(E)) == 2, "typing the account's own address drops the change and sends its code")
forget_sends(ue["id"])
post(e, do="change", email="eli2@example.com", password=PW)
r = post(e, do="change", email="eli3@example.com", password=PW)
check(r.status_code == 429 and "Try again in an hour" in text(r) and user_id(ue["id"])["pending_email"] == "eli2@example.com",
      f"{verify.CHANGES_PER_HOUR} changes an hour, then it waits")
F = "fay@example.com"
f = signup(F)
for _ in range(confirm_step.WRONG_PASSWORDS):
    post(f, do="change", email="fay2@example.com", password="guess")
r = post(f, do="change", email="fay2@example.com", password=PW)
check(r.status_code == 429 and "Too many wrong passwords" in text(r) and user_of(F)["pending_email"] is None,
      "password guesses on the form are limited, and the right one waits too")

# ================================================================ a member already on a plan
print("a member already on a plan")
P = "pat@example.com"
p = signup(P)
up = user_of(P)
plan(P)
r = p.get("/app")
check(r.status_code == 302 and "/app/confirm" in r.headers["Location"], "a member on a plan who never confirmed is asked at the next visit")
r = p.get("/app/confirm")
body = r.get_data(as_text=True)
check('href="/app/library"' in body and 'href="/app/billing"' in body, "the step links to what stays open for them")
r = p.get("/app/library")
check(r.status_code == 200 and "Confirm your email to keep going" in text(r) and '"all": true' in r.get_data(as_text=True),
      "the library stays open, with the banner and its code box")
check(p.get("/app/billing").status_code == 200, "Billing stays open, so a plan can always be canceled")
with appmod.app.test_request_context("/app"):
    g.user, g.ws = up, db.connect().execute("SELECT * FROM workspaces WHERE id=?", (up["workspace_id"],)).fetchone()
    try:
        core.hold_check()
        raised = None
    except Exception as ex:  # noqa: BLE001
        raised = ex
    check(isinstance(raised, verify.ConfirmRequired), "nothing new is made until then")
r = p.post("/app/verify/code", json={"code": code_in(mail_to(P)[0])}, headers={"X-CSRF": csrf(p)})
check(r.status_code == 200 and r.get_json() == {"ok": True, "message": "Email confirmed."}, "the banner's code box confirms it")
r = p.get("/app")
check("/app/confirm" not in (r.headers.get("Location") or ""), "and the app is open again")
with appmod.app.test_request_context("/app"):
    g.user, g.ws = user_of(P), db.connect().execute("SELECT * FROM workspaces WHERE id=?", (up["workspace_id"],)).fetchone()
    core.hold_check()
    check(True, "and they can make things again")

# ================================================================ who is never asked
print("who is never asked")
S = "sam@example.com"
s = signup(S)
run("UPDATE users SET is_admin=1 WHERE email=?", S)
r = s.get("/app/confirm")
check(r.status_code == 200 and "data-confirm-looking" in r.get_data(as_text=True), "a site admin can look at the step")
check("/app/confirm" not in (s.get("/app/plan").headers.get("Location") or ""), "and is never sent to it")
r = post(s, do="check", code="123456")
check(r.status_code == 302 and "already confirmed" in flashes(s)[0], "pressing its button as an admin changes nothing")
V = "vic@example.com"
v = signup(V)
os.environ["RS_EMAIL_VERIFY"] = "video"
check(v.get("/app/plan").status_code == 200, "RS_EMAIL_VERIFY=video: the plan step opens unconfirmed, as before")
os.environ.pop("RS_EMAIL_VERIFY")
check(v.get("/app/plan").status_code == 302, "and back on, it does not")
check(client().get("/app/confirm").status_code == 302, "a visitor is sent to log in")
check(a.get("/pricing").status_code == 200 and v.get("/pricing").status_code == 200, "public pages are not behind the step")
r = v.post("/logout", data={"csrf": csrf(v)})
check(r.status_code == 302, "logging out always works")

# ================================================================ the new look: questions, brief, the step, the plan
print("the new look")
os.environ["UI_V2"] = "1"
N = "nia@example.com"
n = signup(N)
r = n.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome"), "a new account answers the questions first")
check(n.get("/app/welcome").status_code == 200, "the first question opens unconfirmed")
r = n.get("/app/plan")
check(r.headers["Location"].endswith("/app/welcome"), "the plan page still sends a new account to the questions")
r = n.post("/app/welcome", data={"csrf": csrf(n), "goals": ["image"]})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome/profile"), "the first answer is kept")
check(n.get("/app/welcome/profile").status_code == 200, "the second question opens unconfirmed")
r = n.post("/app/welcome/profile", data={"csrf": csrf(n), "first_name": "Nia", "company": "Trail Mug"})
check(r.status_code == 302, "the second answer is kept")
check(n.get("/app/welcome/brand").status_code == 200, "the brand brief opens unconfirmed")
r = n.get("/app/plan")
check(r.status_code == 302 and "/app/confirm" in r.headers["Location"], "then the step comes before the plan")
r = n.get("/app/confirm")
body = r.get_data(as_text=True)
check(r.status_code == 200 and 'class="ob' in body and "data-confirm-step" in body and "ob-btn" in body,
      "in the new look the step sits in the onboarding frame")
r = post(n, do="check", code=code_in(mail_to(N)[0]))
check(r.status_code == 302 and r.headers["Location"].endswith("/app/plan"), "the code confirms and goes on to the plan")
check(n.get("/app/plan").status_code == 200, "and the plan step opens")
os.environ.pop("UI_V2")

# ================================================================ the emails while plans only is on
print("the emails")
sub, txt, htm = nudges._copy_plans(1, 3, True, "https://campaignify.test/m?t=x")
check(sub == "Confirm your email to finish setting up Campaignify" and "free for 3 days" in txt
      and "also confirms this email address" in txt and "Nothing is charged unless you pick a plan." in txt,
      "the reminder to an unconfirmed sign-up says what is left: confirm, then the plan, in the plan step's words")
check("free ads" not in txt and "free credits" not in txt and "nothing renews" not in txt and "free ads" not in htm,
      "it promises no free ads or credits")
sub, txt, _ = nudges._copy_plans(2, 0, False, "https://campaignify.test/m?t=x")
check(sub == "Your Campaignify account is waiting for a plan" and "free for" not in txt and "confirms this email" not in txt
      and "Plans are monthly, and you can cancel any time." in txt,
      "a confirmed sign-up that has had its trial is offered no trial and asked for no confirmation")
G = "gus@example.com"
gq = signup(G)
ug = user_of(G)
long_ago = (datetime.now(timezone.utc) - timedelta(minutes=nudges.STEP_1_MIN + 2)).replace(microsecond=0).isoformat()
run("UPDATE users SET created_at=? WHERE id=?", long_ago, ug["id"])
with db.connect() as conn:
    due = [(u["email"], step) for u, step in nudges.due(conn)]
check((G, 1) in due, "a sign-up that stopped before the plan is due its first reminder")
with db.connect() as conn:
    check(nudges.send(conn, SECRET, user_of(G), 1), "the reminder goes out")
note = mail_to(G, "first_run_nudge_1")
check(len(note) == 1 and note[0]["subject"] == "Confirm your email to finish setting up Campaignify"
      and "free for 3 days" in note[0]["text"] and "2 free" not in note[0]["text"],
      "it is the plans-only reminder, not the free-ads one")
tok = re.search(r"/m\?t=(\S+)", note[0]["text"]).group(1)
fresh = client()
fresh.get(f"/m?t={tok}")
r = fresh.post("/m", data={"csrf": csrf(fresh)})
check(r.status_code == 302 and user_of(G)["email_verified_at"], "opening its link logs in and confirms the address")
check(fresh.get("/app/plan").status_code == 200, "so the plan step is next, with no code to type")
with db.connect() as conn:
    u2 = user_of(F)
    conn.execute("UPDATE users SET verify_nonce=NULL WHERE id=?", (u2["id"],))
    conn.commit()
    before = len(sent)
    verify.send_link(conn, SECRET, user_of(F), first_login=True)
first_mail = sent[before]
check(first_mail["tags"]["kind"] == "verify_signup" and verify.WHY_CONFIRM in first_mail["text"]
      and "credits" not in first_mail["text"], "the confirm-first sign-up email describes no free credits either")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nconfirm step: {passed} checks passed")

"""The done-for-you follow-up on /admin/signups (signup_followup.py; Koby approved the wording on Nov 14 2026), on a
throwaway database. Vendors, the product page and the mail service are faked; nothing leaves the machine.

  * who gets a button: not a payer, not an owner, not a team member, nobody with a finished ad
  * email A: the free first ads made in THEIR workspace (once per workspace and per person), then the email with
    the brackets filled; email B when there is no product page
  * through outreach.send: the daily cap and the do-not-contact list stop it before any ad is made
  * once only: a second press sends nothing and makes nothing; a failed send keeps the ads and retries the email
  * the address never reaches the page

    python tests/test_signup_followup.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_followup_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  STRIPE_SECRET_KEY="sk_test_fr", STRIPE_WEBHOOK_SECRET="whsec_fr", PLANS_ONLY="1", UI_V2="1",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "CLAIMS_CHECK",
          "ANTHROPIC_API_KEY", "GROQ_API_KEY", "MCP_SERVER", "STRIPE_SETUP", "WELCOME_CREDITS"):
    os.environ.pop(k, None)

import admin_funnel  # noqa: E402
import app as appmod  # noqa: E402
import billing  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import first_ads  # noqa: E402
import hf  # noqa: E402
import plan_gate  # noqa: E402
import worker  # noqa: E402
import mailer  # noqa: E402
import outreach  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png(w=400, h=400, rgb=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(b, "PNG")
    b.seek(0)
    return b


def no_vendor(*a, **kw):
    raise AssertionError("this test never calls a vendor")


PAGE = """<!doctype html><html><head><title>Ember Candle | Hearth & Co</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Ember Candle",
"description":"A soy wax candle with amber and cedar. It burns for 40 hours.","image":["https://cdn.hearth.example/ember-1.jpg"],
"brand":{"@type":"Brand","name":"Hearth & Co"}}</script></head><body><h1>Ember Candle</h1></body></html>"""
LINK = "https://hearth.example/p/ember"
submitted = []
hf.submit = lambda model, inputs, webhook_url=None, idempotency_key=None: submitted.append(model) or f"req{len(submitted)}"
hf.upload = lambda data, ctype: "https://files.example/1.png"
hf.status = lambda rid: {"status": "in_progress"}
hf.cancel = lambda rid: hf.STARTED
worker.requests.get = no_vendor
billing._post = billing._get = no_vendor
appmod._maybe_reconcile = lambda ws_id, force=False: 0
fetcher.fetch = lambda url, accept=None: {"url": url, "status": 200, "content_type": "text/html", "text": PAGE}
fetcher.fetch_bytes = lambda url, max_bytes=0, accept=None: {"url": url, "status": 200, "content_type": "image/png",
                                                             "data": png(400).getvalue()}
admin_funnel.ONBOARD_SINCE = "2000-01-01T00:00:00+00:00"
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (user(email)["workspace_id"],)).fetchone()


def signup(email, link=None, landing=None):
    """A sign-up; `link` pasted on the home page first (/signup?url=), `landing` the page its ad click landed on."""
    c = client()
    if landing:
        c.set_cookie("cfy_ft", f"utm_source=google&utm_medium=cpc&lp={landing}&at=1763100000")
    if link:
        c.get("/signup?url=" + link)
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    return c


def questions(c, website=""):
    c.post("/app/welcome", data={"csrf": csrf(c), "goals": ["image", "ugc"]})
    return c.post("/app/welcome/profile", data={"csrf": csrf(c), "first_name": "Kay", "last_name": "Lee",
                                               "company": "Hearth", "website": website})


def read_here(c, url=LINK):
    return c.post("/app/welcome/first", data={"csrf": csrf(c), "do": "import", "url": url})


def make(c):
    return c.post("/app/welcome/first", data={"csrf": csrf(c), "do": "make"})


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def where(r):
    return r.headers.get("Location", "") if r.status_code in (301, 302, 303) else f"({r.status_code})"


def jobs_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def ledger(email, reason=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id",
                            (user(email)["workspace_id"],)).fetchall()
    return [r for r in rows if reason is None or r["reason"] == reason]


def steps_of(email):
    with db.connect() as conn:
        return [r["detail"] for r in conn.execute("SELECT detail FROM site_events WHERE workspace_id=? AND "
                                                  "name='onboard_step' ORDER BY id", (user(email)["workspace_id"],))]


def journey(email):
    with db.connect() as conn:
        return admin_funnel.steps(conn, 30)["per_ws"][user(email)["workspace_id"]]


def is_open(email):
    with appmod.app.test_request_context("/app"), db.connect() as conn:
        return first_ads.free_open(conn, ws_of(email), user(email))


def on_plan(email, code="starter"):
    import time
    with db.connect() as conn:
        wid = user(email)["workspace_id"]
        conn.execute("UPDATE workspaces SET plan=?, plan_status='trialing' WHERE id=?", (code, wid))
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
        plan_gate.start_trial(conn, ws, f"sub_test_{wid}", code, int(time.time()) + 3 * 86400)
        conn.commit()


EACH = catalog.rates()["image"]
COST = first_ads.FREE_IMAGES * EACH
mail = []
mail_fails = {"on": False}


def fake_send(**kw):
    if mail_fails["on"]:
        raise mailer.MailError("the mail service refused it")
    mail.append(kw)
    return f"re_{len(mail)}"


mailer.send = fake_send


def followups(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM signup_followups WHERE user_id=?", (user(email)["id"],)).fetchone()


def press(admin, email):
    return admin.post(f"/admin/signups/{user(email)['id']}/follow-up", data={"csrf": csrf(admin)})


def flashed(c):
    with c.session_transaction() as s:
        return " ".join(m for _cat, m in s.get("_flashes", []))


def cell(body, email):
    """The Follow-up cell of this sign-up's row on /admin/signups."""
    at = body.index(f'data-followup="{user(email)["id"]}"')
    return body[at:body.index("</td>", at)]


def waiting(email, link=True):
    """A sign-up that answered the questions and, with `link`, had its page read, then stopped before making ads."""
    c = signup(email, link="hearth.example/p/ember" if link else None)
    questions(c)
    if link:
        read_here(c)
    return c


admin = signup("boss@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE email='boss@example.com'")
    conn.commit()

print("== who gets a button")
a = waiting("ada@example.com")
b = waiting("bo@example.com", link=False)
payer = waiting("pay@example.com")
on_plan("pay@example.com")
body = page(admin, "/admin/signups")
row = cell(body, "ada@example.com")
check(f"Make {first_ads.FREE_IMAGES} ads + email A" in row, "a sign-up whose page we read: make the ads and send email A")
row = cell(body, "bo@example.com")
check(f"Email B: offer {first_ads.FREE_IMAGES} ads" in row, "no product page: email B")
row = cell(body, "pay@example.com")
check("Paying already." in row and "<button" not in row, "a payer gets no button ")
check("ada@example.com" not in body and "bo@example.com" not in body, "the page still shows no address")
r = press(admin, "pay@example.com")
check(not mail and "Paying already" in flashed(admin), "pressing it anyway for a payer sends nothing")
r = a.post(f"/admin/signups/{user('bo@example.com')['id']}/follow-up", data={"csrf": csrf(a)})
check(r.status_code == 403 and not mail, "a member who isn't an admin can't press it")

print("== email A: the ads in their workspace, then the email")
start = ws_of("ada@example.com")
r = press(admin, "ada@example.com")
check(where(r).endswith("/admin/signups#signups"), "the button comes back to the page")
js = jobs_of("ada@example.com")
check([j["tool"] for j in js] == ["ad_image"] * first_ads.FREE_IMAGES and all(j["workspace_id"] == start["id"] for j in js),
      "the free image ads were made in THEIR workspace")
check(all(j["user_id"] == user("boss@example.com")["id"] for j in js), "made by the admin (no automated ready email to them)")
led = ledger("ada@example.com", first_ads.REASON)
check(len(led) == 1 and led[0]["delta"] == COST and ws_of("ada@example.com")["credits"] == start["credits"],
      "granted exactly what they cost, nothing left over")
check(ws_of("ada@example.com")["first_free_at"], "their free first result is marked taken")
check(len(mail) == 1 and mail[0]["to"] == "ada@example.com", "one email, to their address, looked up by id")
m = mail[0]
check(first_ads.FREE_IMAGES == 2, "two free ads (Koby, Nov 14)")
check(m["subject"] == f"I made {first_ads.FREE_IMAGES} ads from your Ember Candle page", "subject A, with the product")
check(m["text"].startswith("Hi Kay,\n\nYou signed up for Campaignify but didn't get as far as making an ad, so I made "
                           f"two from your product page ({LINK}). Here they are: "
                           "https://campaignify.test/app/welcome/first/result."), "body A, every bracket filled")
check("reply and I'll turn it into a short video with an AI presenter." in m["text"] and "\n\nKoby" in m["text"],
      "the rest of Koby's words, signed Koby")
check("[" not in m["subject"] + m["text"] and "Reply 'no thanks'" in m["text"], "nothing left unfilled; the opt-out footer")
check(m["frm"] == outreach.from_header() and "Koby Stull" in m["frm"], "from the outreach sender")
check(f"Made {first_ads.FREE_IMAGES} free image ads" in flashed(admin) and "ada@example.com" not in flashed(admin), "the page says what happened, without the address")
body = page(admin, "/admin/signups")
row = cell(body, "ada@example.com")
check("emailed" in row and "<button" not in row, "the row now says emailed, with no button")
check(followups("ada@example.com")["kind"] == "A" and followups("ada@example.com")["status"] == "sent", "the send is recorded")
check(where(a.get("/app/welcome/first/result")) == "(200)" and "data-job=" in page(a, "/app/welcome/first/result"),
      "the link in the email opens their ads in their account")

print("== once only")
r = press(admin, "ada@example.com")
check(len(mail) == 1 and len(jobs_of("ada@example.com")) == first_ads.FREE_IMAGES and "Emailed" in flashed(admin),
      "a second press sends nothing and makes nothing")
twin = waiting("ada+again@example.com")
with db.connect() as conn:
    import signup_followup
    check(signup_followup.kind_for(conn, user("ada+again@example.com")) == "B",
          "the same person in a new workspace gets no second set of free ads (email B instead)")

print("== email B")
r = press(admin, "bo@example.com")
check(len(mail) == 2 and mail[1]["to"] == "bo@example.com" and not jobs_of("bo@example.com"), "email B makes nothing")
check(mail[1]["subject"] == f"Want {first_ads.FREE_IMAGES} free ads for Hearth?", "subject B names the brand they typed")
check(mail[1]["text"].startswith("Hi Kay,\n\nYou signed up for Campaignify but didn't get to make an ad. Reply with a "
                                 f"link to one product page and I'll make you {first_ads.FREE_IMAGES} image ads from it, "
                                 "free, usually within a day. No card and nothing to set up.\n\nKoby"), "body B as written")
nobody = signup("nn@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET name='' WHERE email='nn@example.com'")
    conn.commit()
press(admin, "nn@example.com")
check(mail[-1]["subject"] == f"Want {first_ads.FREE_IMAGES} free ads for your product?" and mail[-1]["text"].startswith("Hi there,"),
      "no name and no brand: \"Hi there,\" and \"your product\"")

print("== the guard rails stop it before any ad is made")
d = waiting("dee@example.com")
outreach.suppress(conn_ := db.connect(), "example.org")
conn_.close()
e = waiting("eve@example.org")
r = press(admin, "eve@example.org")
check(len(mail) == 3 and not jobs_of("eve@example.org") and not ledger("eve@example.org"),
      "the do-not-contact list: nothing sent, no ads made")
check("do-not-contact" in flashed(admin) and "eve@example.org" not in flashed(admin), "the reason is shown, the address masked")
with db.connect() as conn:
    for i in range(outreach.DAILY_CAP):
        conn.execute("INSERT INTO outreach_log (to_addr, subject, status, created_at) VALUES (?,?,?,?)",
                     (f"x{i}@else.example", "s", "sent", db.now()))
    conn.commit()
r = press(admin, "dee@example.com")
check(len(mail) == 3 and not jobs_of("dee@example.com") and not ws_of("dee@example.com")["first_free_at"],
      "the daily cap: nothing sent, no ads made, the free first result still open")
check("daily cap" in flashed(admin), "and it says so")
with db.connect() as conn:
    conn.execute("DELETE FROM outreach_log WHERE to_addr LIKE '%@else.example'")
    conn.commit()

print("== a failed send keeps the ads and retries only the email")
mail_fails["on"] = True
r = press(admin, "dee@example.com")
mail_fails["on"] = False
check(len(jobs_of("dee@example.com")) == first_ads.FREE_IMAGES and followups("dee@example.com")["status"] == "failed",
      "the ads were made; the email failed and is recorded as failed")
check("didn't go out" in flashed(admin), "the page says the email didn't go out")
body = page(admin, "/admin/signups")
row = cell(body, "dee@example.com")
check("(retry)" in row and "email A" in row, "the row offers a retry of email A")
r = press(admin, "dee@example.com")
check(len(jobs_of("dee@example.com")) == first_ads.FREE_IMAGES and len(ledger("dee@example.com", first_ads.REASON)) == 1,
      "the retry makes no second set of ads")
check(mail[-1]["to"] == "dee@example.com" and "I made" in mail[-1]["subject"] and followups("dee@example.com")["status"] == "sent",
      "and sends email A")

print("== a finished ad: no follow-up")
f = waiting("fin@example.com")
with db.connect() as conn:
    conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, credits, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (user("fin@example.com")["workspace_id"], user("fin@example.com")["id"],
                                            "ad_image", "done", 0, db.now(), db.now()))
    conn.commit()
before = len(mail)
press(admin, "fin@example.com")
check(len(mail) == before and "finished ad" in flashed(admin), "someone with a finished ad is not emailed")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nALL OK ({passed} checks)")

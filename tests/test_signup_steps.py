"""Where new sign-ups stop (site_events.on_onboard, admin_funnel.steps, /admin/signups): each step of the sign-up flow
is noted once as opened, done or skipped, and the payment step tells Stripe cancels from people who just left.

    python tests/test_signup_steps.py      (tests/test_app.py runs it too)
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
TMP = tempfile.mkdtemp(prefix="rs_steps_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  STRIPE_SECRET_KEY="sk_test_fa", STRIPE_WEBHOOK_SECRET="whsec_fa", PLANS_ONLY="1", UI_V2="1",
                  RS_EMAIL_VERIFY="all", IMPORT_ENRICH="0")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "CLAIMS_CHECK",
          "ANTHROPIC_API_KEY", "GROQ_API_KEY", "MCP_SERVER", "STRIPE_SETUP"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import first_ads  # noqa: E402
import hf  # noqa: E402
import plan_gate  # noqa: E402
import plans  # noqa: E402
import studio_v2  # noqa: E402
import worker  # noqa: E402

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
_ip = [0]
SCRIPT = "Ember Candle. A soy wax candle with amber and cedar."


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.16.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


def signup(email):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    return c


def answer_questions(c):
    c.post("/app/welcome", data={"csrf": csrf(c), "goals": ["image", "ugc"]})
    return c.post("/app/welcome/profile", data={"csrf": csrf(c), "first_name": "Kay", "last_name": "Lee",
                                               "company": "Hearth", "website": ""})


def read_link(c):
    return c.post("/app/welcome/brand", data={"csrf": csrf(c), "do": "import", "url": "hearth.example/p/ember"})


import admin_funnel  # noqa: E402
import site_events  # noqa: E402


def steps_of(email):
    with db.connect() as conn:
        return sorted(r[0] for r in conn.execute("SELECT detail FROM site_events WHERE workspace_id=? AND name='onboard_step'",
                                                 (user(email)["workspace_id"],)))


def journey(email):
    with db.connect() as conn:
        return admin_funnel.steps(conn, 30)["per_ws"][user(email)["workspace_id"]]


def verify_now(email):
    with db.connect() as conn:
        conn.execute("UPDATE users SET email_verified_at=? WHERE email=?", (db.now(), email))
        conn.commit()


admin_funnel.ONBOARD_SINCE = "2000-01-01T00:00:00+00:00"

print("== each step is noted once: opened, done or skipped")
a = signup("a@example.com")
a.get("/app/welcome")
a.get("/app/welcome")
check(steps_of("a@example.com") == ["intent:seen"], "opening the first question twice counts once")
j = journey("a@example.com")
check(j["last"][0] == "intent" and j["state"] == "opened, didn't finish", "and that's where they are")
answer_questions(a)
check(steps_of("a@example.com") == ["intent:done", "intent:seen", "profile:done"], "answering both questions")
a.get("/app/welcome/brand")
check("brief:seen" in steps_of("a@example.com"), "the brief opened")
a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "done"})
check("brief:done" in steps_of("a@example.com"), "and finished")
a.get("/app/confirm")
check("confirm:seen" in steps_of("a@example.com"), "the confirm-email step opened")
check(journey("a@example.com")["last"][0] == "confirm", "they stopped at confirming their email")
verify_now("a@example.com")
a.get("/app/plan")
a.get("/app/plan?canceled=1")
st = steps_of("a@example.com")
check("plan:seen" in st and "plan:checkout_canceled" in st, "the plan step, and coming back from Stripe without paying")
j = journey("a@example.com")
check(j["last"][0] == "plan" and j["state"] == "canceled at Stripe" and "done" in j["steps"]["confirm"],
      "they stopped at payment: canceled at Stripe")

b = signup("b@example.com")
b.post("/app/welcome", data={"csrf": csrf(b), "skip": "1"})
check(steps_of("b@example.com") == ["intent:skipped"] and journey("b@example.com")["state"] == "skipped",
      "skipping the questions is noted as a skip")

c = signup("c@example.com")
answer_questions(c)
c.post("/app/welcome/brand", data={"csrf": csrf(c), "do": "skip"})
check("brief:skipped" in steps_of("c@example.com"), "skipping the brief is noted")
verify_now("c@example.com")
with db.connect() as conn:
    wid = user("c@example.com")["workspace_id"]
    site_events.on_workspace_step(conn, "checkout", wid, detail="plan:starter:trial")
check(journey("c@example.com")["state"] == "left Stripe checkout", "opened Stripe checkout, never paid or came back")

d = signup("d@example.com")
answer_questions(d)
verify_now("d@example.com")
with db.connect() as conn:
    plan_gate.grant_welcome(conn, ws_of("d@example.com"), user("d@example.com"))
    conn.commit()
r = d.get("/app/plan")
check(r.status_code == 200 and "/app/plan/skip" in r.get_data(as_text=True), "on the welcome credits the plan step offers a skip")
r = d.get("/app/plan/skip")
check(r.status_code == 302 and "plan:skipped" in steps_of("d@example.com") and journey("d@example.com")["state"] == "skipped",
      "and skipping it is noted, then the app opens")

print("== the admin's read-out")
e = signup("e@example.com")      # signed up, opened nothing
with db.connect() as conn:
    s = admin_funnel.steps(conn, 30)
rows = {x["key"]: x for x in s["steps"]}
check(s["signups"] == 5 and s["no_steps"] == 1, "five sign-ups, one opened no step")
check(rows["intent"]["reached"] == 4 and rows["intent"]["skipped"] == 1 and rows["intent"]["stopped"] == 1,
      "the first question: four opened it, one skipped and stopped there")
check(rows["plan"]["stopped"] == 3 and s["plan"]["canceled"] == 1 and s["plan"]["left_checkout"] == 1 and s["plan"]["checkout"] == 1,
      "the plan step: three stopped there (one canceled at Stripe, one left checkout, one skipped)")
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE email='a@example.com'")
    conn.commit()
adm = a.get("/admin/signups").get_data(as_text=True)
check("Where they stop" in adm and "Pick a plan" in adm and "left Stripe without paying" in adm, "the admin page shows it")
with db.connect() as conn:
    s2 = admin_funnel.steps(conn, 30)
check(s2["signups"] == 4, "an admin's own account is left out")
with db.connect() as conn:
    check(site_events.on_onboard(conn, 0, "intent", "seen") is None and site_events.on_onboard(conn, 1, "nope", "seen") is None
          and site_events.on_onboard(conn, 1, "intent", "nope") is None, "unknown steps or actions are not recorded")

print(f"\nall {passed} sign-up step checks passed")
shutil.rmtree(TMP, ignore_errors=True)

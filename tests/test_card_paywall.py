"""Card on file at sign-up (card_on_file.py, RS_CARD_REQUIRED) and the out-of-credits
paywall (plan_paywall.py, RS_PLAN_PAYWALL) on a throwaway database. Stripe and the
Anthropic API are faked: nothing leaves the machine and Stripe is never called.

    python tests/test_card_paywall.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_card_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")  # sign-ups here go straight in; test_login.py checks the emailed-link step
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_MARKUP="2.0",
                  RS_EMAIL_VERIFY="0", RS_NUDGE_LOOP="0", ANTHROPIC_API_KEY="sk-ant-test",
                  STRIPE_SECRET_KEY="sk_test_card", STRIPE_WEBHOOK_SECRET="whsec_card")
for k in ("RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "ANTHROPIC_MODEL", "GROQ_API_KEY",
          "RS_STARTER_CREDITS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import card_on_file  # noqa: E402
import catalog  # noqa: E402
import competitors  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import llm  # noqa: E402
import plan_paywall  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def _no_network(*a, **kw):
    raise AssertionError("no real HTTP in these tests (Stripe and Anthropic are faked)")


billing._get = billing._post = _no_network   # Stripe is never called (its two HTTP helpers refuse)
llm.requests.post = _no_network               # the shared requests module: no real HTTP at all
appmod._maybe_reconcile = lambda ws_id, force=False: 0

# ---- fake generation API (an image ad that would cost credits)
hf.submit = lambda model, inputs, webhook_url=None, idempotency_key=None: "req1"
hf.status = lambda rid: {"status": "in_progress"}
hf.upload = lambda data, ctype: "https://files.example/1.png"

# ---------------------------------------------------------------- fake Stripe (the card step only)
stripe = {"customers": [], "sessions": {}, "posts": []}
CARDS = {"visa1": {"fingerprint": "fp_visa_1", "brand": "visa", "last4": "4242"},
         "visa2": {"fingerprint": "fp_visa_2", "brand": "visa", "last4": "1881"},
         "mc3": {"fingerprint": "fp_mc_3", "brand": "mastercard", "last4": "4444"},
         "amex4": {"fingerprint": "fp_amex_4", "brand": "amex", "last4": "0005"},
         "visa5": {"fingerprint": "fp_visa_5", "brand": "visa", "last4": "5556"}}
next_card = {"card": "visa1", "ok": True}


def fake_customer(*, email, metadata, idempotency_key):
    stripe["customers"].append({"email": email, "metadata": metadata, "key": idempotency_key})
    return f"cus_test_{len(stripe['customers'])}"


def fake_setup_session(*, customer, success_url, cancel_url, metadata, client_reference_id, idempotency_key):
    sid = f"cs_test_card_{len(stripe['sessions']) + 1}"
    card = CARDS[next_card["card"]]
    ok = next_card["ok"]
    stripe["sessions"][sid] = {
        "id": sid, "object": "checkout.session", "mode": "setup", "customer": customer, "metadata": dict(
            (k, str(v)) for k, v in metadata.items()),
        "status": "complete" if ok else "open",
        "setup_intent": {"id": f"seti_{sid}", "status": "succeeded" if ok else "requires_payment_method",
                         "payment_method": {"id": f"pm_{sid}", "card": dict(card)} if ok else None}}
    stripe["posts"].append({"customer": customer, "success_url": success_url, "cancel_url": cancel_url,
                            "metadata": metadata, "ref": client_reference_id})
    return {"id": sid, "url": f"https://checkout.stripe.test/{sid}"}


def fake_get_setup_session(sid):
    if sid not in stripe["sessions"]:
        raise billing.BillingError("Stripe 404: no such session")
    return json.loads(json.dumps(stripe["sessions"][sid]))


billing.create_customer, billing.create_setup_session, billing.get_setup_session = \
    fake_customer, fake_setup_session, fake_get_setup_session

# ---------------------------------------------------------------- fake AI: the planner and the competitor research
plan_mode = {"fail": False}


def fake_chat(*, system, messages, tools=None, tool_choice=None, max_tokens=2000, usage=None, temperature=0.7,
              model_id=None, timeout=None):
    if plan_mode["fail"]:
        raise llm.LLMError("the model is down")
    usage.add(llm.model(), 30000, 3000)
    return {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "t1", "name": "save_angles", "input": {"angles": [
            {"name": "SECRETANGLE Morning ritual", "insight": "i", "audience": "a", "why": "w"},
            {"name": "SECRETANGLE Desk fuel", "insight": "i", "audience": "a", "why": "w"}]}},
        {"type": "tool_use", "id": "t2", "name": "finish", "input": {
            "summary": "VISIBLESUMMARY Test two angles on Meta first, then TikTok.",
            "offer": "SECRETOFFER A 30-serving bag.",
            "budget_split": [{"platform": "meta", "share_percent": 60, "note": "VISIBLECHANNEL broad audience first"},
                             {"platform": "tiktok", "share_percent": 40, "note": "SECRETCHANNEL spark ads later"}],
            "kpis": ["SECRETKPI cost per purchase"], "test_plan": "SECRETTEST two weeks",
            "checklist": [{"title": "SECRETCHECK install the pixel", "platform": "meta"}]}}]}


def fake_discover(brand_text, usage, sources):
    usage.add(llm.model(), 20000, 1000, web_searches=2)
    return [{"name": "Rival One", "website": "https://rival1.example/"},
            {"name": "Rival Two", "website": "https://rival2.example/"}]


def fake_research(brand_text, comp, usage, sources, today):
    usage.add(llm.model(), 60000, 3000, web_searches=3)
    return {"name": comp["name"], "website": comp["website"], "summary": "s", "unverified": [],
            "ad_links": competitors.ad_links(comp["name"]),
            "sections": {"offers": [{"text": "SECRETOFFERS 20% off", "sources": [comp["website"]]}]}}


def fake_synthesize(brand_text, comps, usage, sources):
    usage.add(llm.model(), 20000, 2000)
    src = ["https://rival1.example/"]
    return ({"summary": "sum",
             "gaps": [{"text": "INSIGHT1 nobody shows the bag size", "sources": src},
                      {"text": "INSIGHT2 reviews ask about caffeine", "sources": src}],
             "angles": [{"angle": "INSIGHT3 Calm energy", "hook": "No jitters", "why": "w", "sources": src},
                        {"angle": "SECRETINSIGHT4 Office", "hook": "h", "why": "w", "sources": src}],
             "objections": [{"objection": "SECRETINSIGHT5 price", "answer": "a", "sources": src}]}, [])


llm.chat = fake_chat
competitors.discover, competitors.research, competitors.synthesize = fake_discover, fake_research, fake_synthesize

# ---------------------------------------------------------------- helpers
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 200}.{_ip[0] % 200 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"})
    assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:300])
    return c


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                            (email,)).fetchone()


def q(sql, *a):
    with db.connect() as conn:
        return conn.execute(sql, a).fetchall()


def run_sql(sql, *a):
    with db.connect() as conn:
        conn.execute(sql, a)
        conn.commit()


def bal(email):
    return db.balance(ws_of(email))


def add_card(c, card="visa1", ok=True):
    """The member presses Add a card and comes back from (fake) Stripe."""
    next_card.update(card=card, ok=ok)
    r = c.post("/app/card/start", data={"csrf": csrf(c)})
    assert r.status_code == 303, r.status_code
    sid = r.headers["Location"].rsplit("/", 1)[1]
    return sid, c.get(f"/app/card/done?session_id={sid}")


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def page(c, path):
    return c.get(path).get_data(as_text=True)


STARTER = catalog.starter_credits()
DASHES = ("—", "–")

# ================================================================ flags off: nothing changes
print("== flags off")
c0 = signup("off@example.com")
check(bal("off@example.com") == STARTER and STARTER == 44, f"flag off: sign-up gets the {STARTER} starter credits at once")
check(c0.get("/app").status_code == 200, "flag off: Home opens, no card step")
check(c0.get("/app/card").status_code == 302, "flag off: /app/card sends you to Home")
check(c0.get("/app/free-plan").status_code == 404, "paywall off: the free plan page doesn't exist")
run_sql("UPDATE workspaces SET credits=0, plan_credits=0 WHERE id=?", ws_of("off@example.com")["id"])
home = page(c0, "/app")
check("data-paywall-banner" not in home, "paywall off: no lock banner when out of credits")
r = c0.get("/app/campaigns")
check(r.status_code == 402 and "See plans" in r.get_data(as_text=True), "paywall off: the planner shows its upgrade page")
signup_page = page(client(), "/signup")
check("Nothing is charged and nothing renews." in signup_page and "No card" not in signup_page,
      "sign-up page: 'Nothing is charged and nothing renews.' (flag off), no 'No card'")

# ================================================================ card step on
print("== card on file at sign-up")
os.environ["RS_CARD_REQUIRED"] = "1"
check(card_on_file.active(), "RS_CARD_REQUIRED=1 with Stripe keys: the card step is active")
sp = page(client(), "/signup")
check("Next, you save a card. Nothing is charged" in sp and "stops abuse" in sp, "sign-up page says a card comes next")
c1 = signup("new1@example.com")
w1 = ws_of("new1@example.com")
check(bal("new1@example.com") == 0 and w1["card_pending_credits"] == STARTER
      and w1["card_pending_reason"] == "starter credits", "a new sign-up has 0 credits; the 44 wait for the card")
r = c1.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/card"), "Home sends a new account to the card step")
for path in ("/app/brand", "/app/library?x=1", "/app/competitors"):
    rr = c1.get(path)
    if path.startswith("/app/library"):
        check(rr.status_code == 200, "the library still opens")
    else:
        check(rr.status_code == 302 and rr.headers["Location"].endswith("/app/card"), f"{path} goes to the card step")
check(c1.get("/app/billing").status_code == 200, "Billing still opens (a plan or pack needs no card step)")
r = c1.get("/api/jobs", headers={"Accept": "application/json"})
check(r.status_code == 200, "Home's job polling still answers")
r = c1.post("/app/generate", data={"csrf": csrf(c1), "tool": "ad_image"}, headers={"X-Requested-With": "fetch"})
check(r.status_code == 402 and r.get_json().get("card_required") is True, "a fetch that makes something: 402 card_required")
cp = page(c1, "/app/card")
check("Add a card to get your free credits" in cp and f"{STARTER} free credits (2 image ads)" in cp
      and "Why we ask" in cp and "No charge." in cp and "It stops abuse." in cp and "Cancel anytime." in cp
      and "data-card-add" in cp, "the card page: free credits after the card, 'Why we ask' (no charge, abuse, cancel)")
check(not any(d in cp for d in DASHES), "the card page has no em or en dashes")

# card declined
sid, r = add_card(c1, "visa1", ok=False)
post = stripe["posts"][-1]
check(stripe["customers"][-1]["metadata"] == {"app": "campaignify", "kind": "card", "workspace_id": w1["id"]}
      and post["customer"] == "cus_test_1" and post["metadata"]["kind"] == "card"
      and post["success_url"].endswith("/app/card/done?session_id={CHECKOUT_SESSION_ID}"),
      "Add a card: a tagged customer and a setup-mode Checkout Session")
check(r.status_code == 302 and "declined=1" in r.headers["Location"] and bal("new1@example.com") == 0
      and not ws_of("new1@example.com")["card_saved_at"], "card declined: not saved, no credits")
check("wasn't saved" in page(c1, "/app/card?declined=1"), "card declined: the page says so")
check("No card was saved. Nothing was charged." in page(c1, "/app/card?canceled=1"), "canceled: nothing saved, nothing charged")

# card saved
sid, r = add_card(c1, "visa1")
w1 = ws_of("new1@example.com")
check(r.status_code == 302 and r.headers["Location"].endswith("/app") and bal("new1@example.com") == STARTER
      and w1["card_saved_at"] and w1["card_pending_credits"] == 0 and w1["card_label"] == "Visa ending 4242"
      and w1["card_customer_id"] == "cus_test_1", "card saved: the 44 starter credits land, nothing charged")
check(len(stripe["customers"]) == 1, "the second try reuses the same Stripe customer")
check(c1.get("/app").status_code == 200, "with the card saved, Home opens")
c1.get(f"/app/card/done?session_id={sid}")
check(bal("new1@example.com") == STARTER, "coming back twice grants nothing more")
led = q("SELECT delta, reason FROM ledger WHERE workspace_id=?", w1["id"])
check([tuple(x) for x in led] == [(STARTER, "starter credits")], "one ledger row: the starter credits")
# the webhook replays it: still once
body = json.dumps({"id": "evt_card_1", "type": "checkout.session.completed",
                   "data": {"object": dict(stripe["sessions"][sid], setup_intent=f"seti_{sid}")}}).encode()
billing._get = lambda path, params=None: {"id": f"seti_{sid}", "status": "succeeded", "payment_method": {
    "id": "pm_x", "card": CARDS["visa1"]}} if path.startswith("/setup_intents/") else _no_network()
billing.get_setup_session = fake_get_setup_session
r = appmod.app.test_client().post("/webhooks/stripe", data=body,
                                  headers={"Stripe-Signature": billing.sign(body, "whsec_card")})
check(r.status_code == 200 and bal("new1@example.com") == STARTER, "the webhook for the same card grants nothing more")

# one trial per card fingerprint
c2 = signup("new2@example.com")
sid, r = add_card(c2, "visa1")
w2 = ws_of("new2@example.com")
check(w2["card_saved_at"] and bal("new2@example.com") == 0 and w2["card_pending_credits"] == 0,
      "the same card on a second account: saved, but no starter credits")
check(any("already had free credits on another account" in m for m in flashes(c2)), "the second account is told why")
check(c2.get("/app").status_code == 200, "it can still use the app (buy a pack or pick a plan)")
# one trial per email (Gmail dots and +tags count as the same address)
ca = signup("jane.doe@gmail.com")
add_card(ca, "mc3")
check(bal("jane.doe@gmail.com") == STARTER, "a first Gmail account gets its credits")
cb = signup("janedoe+ads@gmail.com")
add_card(cb, "amex4")
check(bal("janedoe+ads@gmail.com") == 0, "the same Gmail address with dots and a +tag, another card: no credits")
check(q("SELECT COUNT(*) FROM card_trials WHERE email_norm='janedoe@gmail.com' AND trial=1")[0][0] == 1,
      "card_trials records one trial for that person")

# the webhook alone (the member closed the tab on Stripe's page)
billing._get = _no_network
c3 = signup("tabclosed@example.com")
next_card.update(card="visa2", ok=True)
r = c3.post("/app/card/start", data={"csrf": csrf(c3)})
sid3 = r.headers["Location"].rsplit("/", 1)[1]
ev = json.dumps({"id": "evt_card_3", "type": "checkout.session.completed",
                 "data": {"object": {"id": sid3, "object": "checkout.session", "mode": "setup", "status": "complete",
                                     "setup_intent": f"seti_{sid3}",
                                     "metadata": stripe["sessions"][sid3]["metadata"]}}}).encode()
r = appmod.app.test_client().post("/webhooks/stripe", data=ev, headers={"Stripe-Signature": billing.sign(ev, "whsec_card")})
check(r.status_code == 200 and r.get_json().get("card") == "saved" and bal("tabclosed@example.com") == STARTER,
      "the webhook saves the card and grants the credits")
check(c3.get("/app").status_code == 200, "and the account opens straight into Home")
other = json.dumps({"id": "evt_other", "type": "checkout.session.completed",
                    "data": {"object": {"id": "cs_other", "mode": "setup", "metadata": {"app": "morning"}}}}).encode()
r = appmod.app.test_client().post("/webhooks/stripe", data=other, headers={"Stripe-Signature": billing.sign(other, "whsec_card")})
check(r.status_code == 200 and r.get_json().get("ignored"), "another app's setup session is ignored")

# Continue with Google: the same card step after sign-up (accounts.self_serve_signup)
import accounts  # noqa: E402
with appmod.app.test_request_context("/auth/google/callback", environ_base={"REMOTE_ADDR": "10.99.0.1"}):
    accounts.self_serve_signup("g.user@example.com", "G", "x-unusable", appmod.request, "10.99.0.1", appmod.app.secret_key,
                               auth_provider="google", verified=True)
wg = ws_of("g.user@example.com")
check(wg["card_pending_credits"] == STARTER and db.balance(wg) == 0, "a Google sign-up's credits wait for the card too")

# ================================================================ existing free accounts without a card
print("== existing free accounts")
os.environ.pop("RS_CARD_REQUIRED")
c4 = signup("old@example.com")
check(bal("old@example.com") == STARTER, "made before the card step: starter credits at sign-up")
w4 = ws_of("old@example.com")
run_sql("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", w4["id"], q("SELECT id FROM users WHERE email='old@example.com'")[0][0],
        "ad_image", "an old ad", "{}", "done", 22, 0.1, db.now(), db.now())
os.environ["RS_CARD_REQUIRED"] = "1"
r = c4.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/card"), "on its next visit it goes to the card step")
cp = page(c4, "/app/card")
check("Add a card to keep making ads" in cp and "Your ads, products and credits are all still here" in cp
      and "Go to your library" in cp and "Why we ask" in cp, "the page: data kept, add a card to make new things")
check(c4.get("/app/library").status_code == 200 and c4.get("/app/billing").status_code == 200,
      "the library and Billing still open")
r = c4.post("/app/generate", data={"csrf": csrf(c4), "tool": "ad_image"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/card"), "making something new goes to the card step")
check(q("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", w4["id"])[0][0] == 1, "nothing new was made")
# the API path (MCP and anything that skips the page gate): core.hold_check refuses
with appmod.app.test_request_context("/mcp"):
    appmod.g.ws, appmod.g.user = ws_of("old@example.com"), q("SELECT * FROM users WHERE email='old@example.com'")[0]
    try:
        core.hold_check()
        refused = False
    except card_on_file.CardRequired:
        refused = True
check(refused, "core.hold_check raises CardRequired for a free workspace without a card (MCP, API)")
add_card(c4, "visa5")
w4 = ws_of("old@example.com")
check(w4["card_saved_at"] and bal("old@example.com") == STARTER, "card added: no second set of starter credits")
check(q("SELECT trial FROM card_trials WHERE workspace_id=?", w4["id"])[0][0] == 1,
      "its card counts as having had the trial")
check(c4.get("/app").status_code == 200, "and Home opens again")

# paid, comped, pack buyers and admins are never asked
for email, sql in (("comp@example.com", "UPDATE workspaces SET plan_override='growth' WHERE id=?"),
                   ("pack@example.com", "INSERT INTO purchases (workspace_id, pack, credits, amount_cents, currency, "
                                        "status, created_at) VALUES (?, 'small', 500, 900, 'usd', 'paid', '2026-11-01')"),
                   ("sub@example.com", "INSERT INTO plan_invoices (invoice_id, workspace_id, plan, credits, "
                                       "amount_cents, created_at) VALUES ('in_paid_1', ?, 'starter', 0, 2900, "
                                       "'2026-10-01')")):
    os.environ.pop("RS_CARD_REQUIRED")
    cx = signup(email)
    run_sql(sql, ws_of(email)["id"])
    os.environ["RS_CARD_REQUIRED"] = "1"
    check(cx.get("/app").status_code == 200, f"{email.split('@')[0]}: no card step")
# L-1: a Stripe customer id alone (a 100% promotion code, a first invoice that never paid) is not an exemption,
# and neither is a $0 plan invoice
for email, sql in (("custonly@example.com", "UPDATE workspaces SET stripe_customer_id='cus_old_sub' WHERE id=?"),
                   ("zeroinv@example.com", "INSERT INTO plan_invoices (invoice_id, workspace_id, plan, credits, "
                                           "amount_cents, created_at) VALUES ('in_zero_1', ?, 'starter', 0, 0, "
                                           "'2026-10-01')")):
    os.environ.pop("RS_CARD_REQUIRED")
    cx = signup(email)
    run_sql(sql, ws_of(email)["id"])
    os.environ["RS_CARD_REQUIRED"] = "1"
    r = cx.get("/app")
    check(r.status_code == 302 and r.headers["Location"].endswith("/app/card"),
          f"{email.split('@')[0]}: back on Free with no real payment, the card step is asked (L-1)")
os.environ.pop("RS_CARD_REQUIRED")
cad = signup("admin@example.com")
run_sql("UPDATE users SET is_admin=1 WHERE email='admin@example.com'")
os.environ["RS_CARD_REQUIRED"] = "1"
check(cad.get("/app").status_code == 200, "a site admin: no card step")

# turning the flag off releases credits still waiting for a card
cw = signup("waiting@example.com")
check(bal("waiting@example.com") == 0, "waiting for its card")
os.environ.pop("RS_CARD_REQUIRED")
check(cw.get("/app").status_code == 200 and bal("waiting@example.com") == STARTER
      and ws_of("waiting@example.com")["card_pending_credits"] == 0, "flag off: the waiting credits land on the next visit")

# ================================================================ the out-of-credits paywall
print("== out-of-credits paywall")
os.environ["RS_PLAN_PAYWALL"] = "1"
cp1 = signup("plan@example.com")
wp = ws_of("plan@example.com")
check(not plan_paywall.applies(wp), "with credits left, no lock")
check("data-paywall-banner" not in page(cp1, "/app"), "no banner while there are credits")
fp = page(cp1, "/app/free-plan")
check("for accounts that are out of credits" in fp, "the free plan page says it's for accounts out of credits")
run_sql("UPDATE workspaces SET credits=?, plan_credits=0 WHERE id=?", plan_paywall.min_price() - 1, wp["id"])
check(plan_paywall.applies(ws_of("plan@example.com")), "less than one image ad left: out of credits")
home = page(cp1, "/app")
check("data-paywall-banner" in home and "Get an active subscription to unlock" in home and "lock-ic" in home
      and "/app/billing#plans" in home and "Make my free plan" not in home,
      "every app page: the lock, 'Get an active subscription to unlock', See plans (no free plan: email unconfirmed)")
r = cp1.get("/app/campaigns")
check(r.status_code == 402, "unconfirmed: the planner opens on its upgrade page")
# everything else that costs credits shows the same lock
r = cp1.post("/app/generate", data={"csrf": csrf(cp1), "tool": "ad_image", "template": "t", "brief": "a mug on a desk"})
body = r.get_data(as_text=True)
check(r.status_code == 402 and "Get an active subscription to unlock" in body and "lock-ic" in body
      and "data-paywall-locked" in body, "making an ad with no credits: the lock page (402)")
run_sql("UPDATE workspaces SET brand_name='Brew Co' WHERE id=?", wp["id"])
r = cp1.post("/app/competitors", data={"csrf": csrf(cp1), "competitor": ["rival.example"]},
             headers={"X-Requested-With": "fetch"})
check(r.status_code == 402 and r.get_json() == {"error": "Get an active subscription to unlock", "paywall": True,
                                                 "plans": "/app/billing#plans"}, "competitor research: the same lock (JSON)")
# L-5: an unconfirmed email (and no card that earned the trial) gets no free plan
fp = page(cp1, "/app/free-plan")
check("data-free-plan-refused" in fp and "Confirm your email" in fp and "data-free-plan-form" not in fp,
      "unconfirmed email, no card: the free plan page asks for a confirmed email first")
r = cp1.post("/app/free-plan", data={"csrf": csrf(cp1), "offer": "Brew Co coffee", "goal": "sales"})
check(r.status_code == 302 and not q("SELECT * FROM campaigns WHERE workspace_id=?", wp["id"])
      and not q("SELECT * FROM free_plans WHERE workspace_id=?", wp["id"]), "and a POST makes nothing")
check(cp1.get("/app/campaigns").status_code == 402, "the planner's door is the upgrade page, not the free plan")
run_sql("UPDATE users SET email_verified_at=? WHERE email='plan@example.com'", db.now())
r = cp1.get("/app/campaigns")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/free-plan"), "confirmed: the planner opens on the free plan")
check("Make my free plan" in page(cp1, "/app"), "and the banner offers the free plan")
# the free plan
spent_before = q("SELECT COUNT(*) FROM ledger WHERE workspace_id=?", wp["id"])[0][0]
bal_before = bal("plan@example.com")
r = cp1.post("/app/free-plan", data={"csrf": csrf(cp1), "offer": "Brew Co coffee", "audience": "remote workers",
                                     "goal": "sales", "platforms": ["meta", "tiktok"]})
camp = q("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC", wp["id"])[0]
rep = q("SELECT * FROM competitor_reports WHERE workspace_id=? ORDER BY id DESC", wp["id"])[0]
check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/free-plan/{camp['id']}") and camp["free_plan"] == 1
      and camp["status"] == "ready", "the free plan runs: competitor research, then the planner")
check(rep["status"] == "ready" and rep["credits"] == 0 and rep["usd_cost"] > 0 and json.loads(rep["inputs_json"])["free"],
      "the research cost us real money and charged 0 credits")
check(camp["credits"] == 0 and camp["cost_usd"] > 0, "the planning cost us real money and charged 0 credits")
check(bal("plan@example.com") == bal_before and q("SELECT COUNT(*) FROM ledger WHERE workspace_id=?", wp["id"])[0][0]
      == spent_before, "the balance and the ledger are untouched")
fr = q("SELECT * FROM free_plans WHERE workspace_id=?", wp["id"])
check(len(fr) == 1 and fr[0]["campaign_id"] == camp["id"] and fr[0]["report_id"] == rep["id"]
      and fr[0]["status"] == "ready" and fr[0]["email_norm"] == "plan@example.com",
      "the free plan is recorded, with the member's email")
# the part-locked plan page
show = page(cp1, f"/app/free-plan/{camp['id']}")
check("VISIBLESUMMARY" in show and "VISIBLECHANNEL" in show and "Meta" in show and "60%" in show,
      "the plan page shows the summary and the first channel's recommendation")
check(all(f"INSIGHT{i}" in show for i in (1, 2, 3)) and "SECRETINSIGHT4" not in show and "SECRETINSIGHT5" not in show,
      "it shows the top 3 competitor insights, not the rest")
check(not re.search(r"SECRET(ANGLE|OFFER|CHANNEL|KPI|TEST|CHECK|OFFERS)", show),
      "angles, the offer, other channels, KPIs, the test plan and the checklist stay hidden")
check("Get an active subscription to unlock" in show and "lock-ic" in show and "/app/billing#plans" in show
      and "data-locked" in show and "Launch checklist" in show, "the rest sits behind the lock, linking to plans")
check(not any(d in show for d in DASHES), "the plan page has no em or en dashes")
r = cp1.get(f"/app/campaigns/{camp['id']}")
check(r.status_code == 402, "the full plan page stays on the planner's paywall")
r = cp1.get(f"/app/competitors/{rep['id']}")
check(r.status_code == 402 and "Get an active subscription to unlock" in r.get_data(as_text=True)
      and "SECRETOFFERS" not in r.get_data(as_text=True), "the free plan's full competitor report is locked")
check(cp1.get(f"/app/competitors/{rep['id']}.md").status_code == 402, "and so is its Markdown download")
# one per 30 days
r = cp1.post("/app/free-plan", data={"csrf": csrf(cp1), "offer": "Again", "goal": "sales"})
check(r.status_code == 302 and len(q("SELECT * FROM campaigns WHERE workspace_id=?", wp["id"])) == 1,
      "a second free plan in the same 30 days is refused")
fp = page(cp1, "/app/free-plan")
nxt = (datetime.fromisoformat(fr[0]["created_at"]) + timedelta(days=30)).date().isoformat()
check(f"The next one opens on <b>{nxt}</b>" in fp and "Open my free plan" in fp, "the page says when the next one opens")
check("Make my free plan" not in page(cp1, "/app") and "data-paywall-banner" in page(cp1, "/app"),
      "the banner keeps the lock, without the free plan button")
check(cp1.get("/app/campaigns").status_code == 402, "the planner's door is the upgrade page again")
with db.connect() as conn:
    check(plan_paywall.claim(conn, plan_paywall.who(conn, ws_of("plan@example.com"), q(
        "SELECT * FROM users WHERE email='plan@example.com'")[0])) is None, "claim() refuses inside the window (atomic insert)")
    conn.rollback()
old = (datetime.now(timezone.utc) - timedelta(days=31)).replace(microsecond=0).isoformat()
run_sql("UPDATE free_plans SET created_at=? WHERE workspace_id=?", old, wp["id"])
check("data-free-plan-form" in page(cp1, "/app/free-plan"), "31 days later the next free plan is available")
# a run that produces nothing gives the month's plan back
plan_mode["fail"] = True
cp1.post("/app/free-plan", data={"csrf": csrf(cp1), "offer": "Brew Co decaf", "goal": "sales"})
plan_mode["fail"] = False
failed = q("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC", wp["id"])[0]
check(failed["status"] == "failed" and failed["free_plan"] == 1
      and q("SELECT COUNT(*) FROM free_plans WHERE workspace_id=? AND created_at>? AND status!='failed'",
            wp["id"], old)[0][0] == 0, "a failed free plan is given back")
check("data-free-plan-form" in page(cp1, "/app/free-plan"), "so the member can try again")
check("Try again" in page(cp1, f"/app/free-plan/{failed['id']}"), "the failed plan's page offers to try again")
# L-7: at most 2 retries per 30 days (3 failed tries), then it waits like a made plan
plan_mode["fail"] = True
cp1.post("/app/free-plan", data={"csrf": csrf(cp1), "offer": "Brew Co decaf 2", "goal": "sales"})
check("data-free-plan-form" in page(cp1, "/app/free-plan"), "a second failure: 1 retry left")
cp1.post("/app/free-plan", data={"csrf": csrf(cp1), "offer": "Brew Co decaf 3", "goal": "sales"})
plan_mode["fail"] = False
fails = q("SELECT * FROM free_plans WHERE workspace_id=? AND status='failed' AND created_at>?", wp["id"], old)
check(len(fails) == 3, "3 failed tries recorded")
fp = page(cp1, "/app/free-plan")
check("data-free-plan-form" not in fp and "used this month's tries" in fp
      and ((datetime.fromisoformat(fails[0]["created_at"]) + timedelta(days=30)).date().isoformat()) in fp,
      "after 3 failed tries the form is gone and the page says when the next try opens")
last_failed = q("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC", wp["id"])[0]
lp = page(cp1, f"/app/free-plan/{last_failed['id']}")
check("data-free-plan-tries-used" in lp and "Try again" not in lp, "the failed plan's page no longer offers a retry")
n_camps = len(q("SELECT * FROM campaigns WHERE workspace_id=?", wp["id"]))
cp1.post("/app/free-plan", data={"csrf": csrf(cp1), "offer": "Brew Co decaf 4", "goal": "sales"})
check(len(q("SELECT * FROM campaigns WHERE workspace_id=?", wp["id"])) == n_camps, "a 4th try is refused")
with db.connect() as conn:
    check(plan_paywall.claim(conn, plan_paywall.who(conn, ws_of("plan@example.com"),
                                                   q("SELECT * FROM users WHERE email='plan@example.com'")[0])) is None,
          "claim() refuses after 3 failed tries (atomic)")
    conn.rollback()
# subscribed workspaces never see the paywall
cs = signup("subscribed@example.com")
run_sql("UPDATE workspaces SET plan_override='starter', credits=0, plan_credits=0 WHERE id=?", ws_of("subscribed@example.com")["id"])
check(not plan_paywall.applies(ws_of("subscribed@example.com")) and "data-paywall-banner" not in page(cs, "/app"),
      "a subscribed workspace out of credits: no lock banner")
check(cs.get("/app/free-plan").status_code == 302, "and the free plan page sends it to the planner")
# a subscription unlocks everything already made
run_sql("UPDATE workspaces SET plan_override='starter' WHERE id=?", wp["id"])
r = cp1.get(f"/app/free-plan/{camp['id']}")
check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/campaigns/{camp['id']}"),
      "subscribed: the free plan page opens the full plan")
full = page(cp1, f"/app/campaigns/{camp['id']}")
check("SECRETANGLE Morning ritual" in full and "SECRETKPI" in full and "SECRETCHECK" in full and "SECRETCHANNEL" in full,
      "the full plan: angles, KPIs, checklist, every channel")
r = cp1.get(f"/app/competitors/{rep['id']}")
check(r.status_code == 200 and "SECRETOFFERS" in r.get_data(as_text=True), "and the full competitor report")
check("data-paywall-banner" not in page(cp1, "/app"), "no lock banner once subscribed")
os.environ.pop("RS_PLAN_PAYWALL")

# ================================================================ M2 re-audit: the free plan with the card step on
print("== free plan per person (card step and paywall on)")
os.environ["RS_PLAN_PAYWALL"] = "1"
os.environ["RS_CARD_REQUIRED"] = "1"
CARDS["visa6"] = {"fingerprint": "fp_visa_6", "brand": "visa", "last4": "6006"}
CARDS["visa7"] = {"fingerprint": "fp_visa_7", "brand": "visa", "last4": "7007"}


def out_of_credits(email):
    run_sql("UPDATE workspaces SET credits=?, plan_credits=0 WHERE id=?", plan_paywall.min_price() - 1, ws_of(email)["id"])


# M-1: a reused card (saved, 0 credits) is out of credits at once, but gets no free plan
cf = signup("farm1@example.com")
add_card(cf, "visa1")                        # the card new1@ already had its trial on
check(bal("farm1@example.com") == 0 and plan_paywall.applies(ws_of("farm1@example.com")),
      "a reused card: saved with 0 credits, so out of credits at once")
run_sql("UPDATE users SET email_verified_at=? WHERE email='farm1@example.com'", db.now())   # even confirmed
fp = page(cf, "/app/free-plan")
check("data-free-plan-refused" in fp and "already had a free trial on another account" in fp
      and "data-free-plan-form" not in fp, "the free plan page says this card or email already had a trial (M-1)")
r = cf.post("/app/free-plan", data={"csrf": csrf(cf), "offer": "Farmed plan", "goal": "sales"})
check(r.status_code == 302 and not q("SELECT * FROM free_plans WHERE workspace_id=?", ws_of("farm1@example.com")["id"]),
      "and a POST claims nothing")
check(cf.get("/app/campaigns").status_code == 402 and "Make my free plan" not in page(cf, "/app"),
      "the planner door and the banner don't offer it either")
# a card that earned the trial: the free plan works without a confirmed email
cg = signup("good1@example.com")
add_card(cg, "visa6")
check(bal("good1@example.com") == STARTER, "a new card earns the starter credits")
out_of_credits("good1@example.com")
check("data-free-plan-form" in page(cg, "/app/free-plan"), "its card earned the trial: the free plan form shows")
r = cg.post("/app/free-plan", data={"csrf": csrf(cg), "offer": "Good plan", "goal": "sales"})
row = q("SELECT * FROM free_plans WHERE workspace_id=?", ws_of("good1@example.com")["id"])
check(len(row) == 1 and row[0]["fingerprint"] == "fp_visa_6" and row[0]["status"] == "ready",
      "the free plan is made and recorded with the card's fingerprint")
# the same card on an account that already had its starter credits (trial=1 by the old grant): one plan per card
os.environ.pop("RS_CARD_REQUIRED")
ch = signup("had@example.com")             # made before the card step: starter credits at sign-up
os.environ["RS_CARD_REQUIRED"] = "1"
add_card(ch, "visa6")
check(q("SELECT trial FROM card_trials WHERE workspace_id=?", ws_of("had@example.com")["id"])[0][0] == 1,
      "that account's card row counts as a trial (it had the credits already)")
out_of_credits("had@example.com")
fp = page(ch, "/app/free-plan")
check("data-free-plan-form" not in fp and "The next one opens on" in fp,
      "the card's free plan was used on another account in the last 30 days: none here (per fingerprint)")
with db.connect() as conn:
    k = plan_paywall.who(conn, ws_of("had@example.com"), q("SELECT * FROM users WHERE email='had@example.com'")[0])
    check(k["fingerprint"] == "fp_visa_6" and plan_paywall.claim(conn, k) is None, "claim() refuses on the fingerprint")
    conn.rollback()

# L-6: spend in flight counts back: a pack holder can't dip under one image ad and claim
cl = signup("dip@example.com")
add_card(cl, "visa7")
wd = ws_of("dip@example.com")
run_sql("UPDATE workspaces SET credits=5, plan_credits=0 WHERE id=?", wd["id"])
run_sql("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", wd["id"], q("SELECT id FROM users WHERE email='dip@example.com'")[0][0],
        "ugc_video", "an expensive video", "{}", "pending", 400, 1.0, db.now(), db.now())
check(plan_paywall.applies(ws_of("dip@example.com")), "5 credits left with a 400-credit video queued: the lock applies")
fp = page(cl, "/app/free-plan")
check("data-free-plan-busy" in fp and "data-free-plan-form" not in fp, "but the free plan waits for the video (L-6)")
cl.post("/app/free-plan", data={"csrf": csrf(cl), "offer": "Dipped", "goal": "sales"})
check(not q("SELECT * FROM free_plans WHERE workspace_id=?", wd["id"]), "a POST claims nothing while it runs")
run_sql("UPDATE jobs SET status='failed' WHERE workspace_id=?", wd["id"])
run_sql("INSERT INTO credit_holds (workspace_id, amount, label, ref, status, created_at, updated_at) "
        "VALUES (?, 300, 'Competitor research', 'competitor:1', 'held', ?, ?)", wd["id"], db.now(), db.now())
check("data-free-plan-busy" in page(cl, "/app/free-plan"), "an open credit hold counts back the same way")
run_sql("UPDATE credit_holds SET status='released' WHERE workspace_id=?", wd["id"])
check("data-free-plan-form" in page(cl, "/app/free-plan"), "once nothing is in flight, the free plan opens")

# L-8: while a free plan is being made, the step list shows no angle names or the model's words
cid = q("SELECT campaign_id FROM free_plans WHERE workspace_id=?", ws_of("good1@example.com")["id"])[0][0]
run_sql("UPDATE campaigns SET status='planning', updated_at=?, steps_json=? WHERE id=?", db.now(), json.dumps([
    {"kind": "angles", "title": "Proposed 2 angles", "detail": "SECRETANGLE A · SECRETANGLE B"},
    {"kind": "creative", "title": "Wrote hooks and a script for \u201cSECRETANGLE A\u201d"},
    {"kind": "ad", "title": "Lined up an image ad for \u201cSECRETANGLE B\u201d"},
    {"kind": "note", "title": "SECRETNOTE I will lead with the morning angle"}]), cid)
pl = page(cg, f"/app/free-plan/{cid}")
check("SECRETANGLE" not in pl and "SECRETNOTE" not in pl and "Wrote hooks and scripts" in pl
      and "Lined up the ads" in pl and "Planning the campaign" in pl, "planning steps are generic labels (L-8)")
run_sql("UPDATE campaigns SET status='ready' WHERE id=?", cid)

# M-2: the card step's checkout start: per network, app-wide, and a confirmed email past the sign-up limit
import time as _time  # noqa: E402
cm = signup("cardtest@example.com")
ipm = cm.environ_base["REMOTE_ADDR"]
for _ in range(card_on_file.TRIES_PER_IP_HOUR):
    run_sql("INSERT INTO throttle (key, at) VALUES (?, ?)", f"card-ip:{ipm}", _time.time())
n0 = len(stripe["sessions"])
r = cm.post("/app/card/start", data={"csrf": csrf(cm)})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/card") and len(stripe["sessions"]) == n0
      and any("Too many tries" in m for m in flashes(cm)), "card start: refused past the per-network limit (M-2)")
run_sql("DELETE FROM throttle WHERE key=?", f"card-ip:{ipm}")
os.environ["RS_CARD_STARTS_PER_HOUR"] = "1"
run_sql("DELETE FROM throttle WHERE key='card-all'")
run_sql("INSERT INTO throttle (key, at) VALUES ('card-all', ?)", _time.time())
r = cm.post("/app/card/start", data={"csrf": csrf(cm)})
check(r.status_code == 302 and len(stripe["sessions"]) == n0 and any("busy" in m for m in flashes(cm)),
      "card start: refused past the app-wide limit (RS_CARD_STARTS_PER_HOUR)")
os.environ.pop("RS_CARD_STARTS_PER_HOUR")
for _ in range(accounts.SIGNUPS_PER_HOUR):
    run_sql("INSERT INTO throttle (key, at) VALUES (?, ?)", f"signup:{ipm}", _time.time())
r = cm.post("/app/card/start", data={"csrf": csrf(cm)})
check(r.status_code == 302 and len(stripe["sessions"]) == n0 and any("Confirm your email first" in m for m in flashes(cm)),
      "card start: a network past the sign-up limit needs a confirmed email first")
run_sql("UPDATE users SET email_verified_at=? WHERE email='cardtest@example.com'", db.now())
next_card.update(card="visa7", ok=True)
r = cm.post("/app/card/start", data={"csrf": csrf(cm)})
check(r.status_code == 303 and len(stripe["sessions"]) == n0 + 1, "confirmed: the card checkout opens")
check(q("SELECT COUNT(*) FROM throttle WHERE key=?", f"card-ip:{ipm}")[0][0] == 1
      and q("SELECT COUNT(*) FROM throttle WHERE key='card-all'")[0][0] >= 1, "each start counts per network and app-wide")
os.environ.pop("RS_CARD_REQUIRED")
os.environ.pop("RS_PLAN_PAYWALL")

# ================================================================ copy
print("== copy")
for path in ("/", "/pricing", "/signup", "/arcads-alternative", "/makeugc-alternative", "/predis-ai-alternative",
             "/llms.txt"):
    html = page(client(), path)
    check("No card" not in html and "no card," not in html and "with no card" not in html,
          f"{path}: no 'no card' claim")
check("Make my 2 free ads" not in open(os.path.join(HERE, "..", "web", "_first_ad.html")).read()
      and "Make 2 ads with your free credits" in open(os.path.join(HERE, "..", "web", "_first_ad.html")).read(),
      "the first-run button says 'Make 2 ads with your free credits'")
for name in ("card.html", "free_plan.html", "free_plan_new.html", "paywall_locked.html", "_paywall_banner.html"):
    txt = open(os.path.join(HERE, "..", "web", name)).read()
    check(not any(d in txt for d in DASHES), f"web/{name}: no em or en dashes")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

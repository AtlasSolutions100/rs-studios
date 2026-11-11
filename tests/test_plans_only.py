"""Plans only (plan_gate.py, PLANS_ONLY) and the percent-off price (plans.sale, PLAN_SALE) on a throwaway
database. Stripe is faked: nothing leaves the machine.

    python tests/test_plans_only.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_plansonly_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", STRIPE_SECRET_KEY="sk_test_po", STRIPE_WEBHOOK_SECRET="whsec_po",
                  PLANS_ONLY="1", PLAN_SALE="1")
for k in ("RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "UI_V2", "HOME_V2",
          "STRIPE_SETUP"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import catalog  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402
import plan_gate  # noqa: E402
import plans  # noqa: E402
import public_site  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


# ---------------------------------------------------------------- fake Stripe
posts, sessions, refuse_note, cards = [], {}, [False], {}


def fake_post(path, form, idempotency_key=None):
    posts.append((path, list(form), idempotency_key))
    if path != "/checkout/sessions":
        raise AssertionError(f"unexpected Stripe call {path}")
    if refuse_note[0] and any(k.startswith("custom_text") for k, _ in form):
        raise billing.BillingError("Stripe 400: unknown parameter", 400, "parameter_unknown",
                                   "custom_text[submit][message]")
    sid = f"cs_test_{len(posts)}"
    return {"id": sid, "url": f"https://checkout.stripe.test/c/{sid}"}


def fake_get(path, params=None):
    if path.startswith("/checkout/sessions/"):
        sid = path.rsplit("/", 1)[1]
        if sid in sessions:
            return sessions[sid]
        raise billing.BillingError("Stripe 404: No such checkout.session", 404, "resource_missing", "id")
    if path == "/invoice_payments":
        return {"object": "list", "data": []}
    if path.startswith("/subscriptions/"):          # the card a trial was started with (one trial per card)
        sub = path.rsplit("/", 1)[1]
        return {"id": sub, "default_payment_method": {"id": f"pm_{sub}", "card": {"fingerprint": cards.get(sub, f"fp_{sub}")}}}
    raise AssertionError(f"unexpected Stripe read {path}")


billing._post, billing._get = fake_post, fake_get
appmod._maybe_reconcile = lambda ws_id, force=False: 0

# ---------------------------------------------------------------- helpers
_ip, _n = [0], [0]
NOW = int(time.time())
IN_3_DAYS, IN_33_DAYS, IN_63_DAYS = NOW + 3 * 86400, NOW + 33 * 86400, NOW + 63 * 86400
PRICE = {c: plans.PLANS[c]["price_id"] for c in ("starter", "growth", "scale")}


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.11.{_ip[0] // 200}.{_ip[0] % 200 + 1}"
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


def user_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def q(sql, *a):
    with db.connect() as conn:
        return conn.execute(sql, a).fetchall()


def run_sql(sql, *a):
    with db.connect() as conn:
        conn.execute(sql, a)
        conn.commit()


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def page(c, path):
    return c.get(path).get_data(as_text=True)


def deliver(typ, obj):
    _n[0] += 1
    body = json.dumps({"id": f"evt_po{_n[0]}", "type": typ, "data": {"object": obj}}).encode()
    return appmod.app.test_client().post("/webhooks/stripe", data=body, content_type="application/json",
                                         headers={"Stripe-Signature": billing.sign(body, "whsec_po")})


def meta(email, code, trial=3):
    w, u = ws_of(email), user_of(email)
    m = {"app": "campaignify", "kind": "plan", "workspace_id": str(w["id"]), "plan": code, "user_id": str(u["id"])}
    if trial:
        m["trial_days"] = str(trial)
    return m


def ev_checkout(email, sub, code, trial=3):
    return deliver("checkout.session.completed", {
        "id": f"cs_{sub}", "object": "checkout.session", "mode": "subscription", "customer": f"cus_{sub}",
        "subscription": sub, "status": "complete", "payment_status": "no_payment_required" if trial else "paid",
        "metadata": meta(email, code, trial)})


def ev_invoice(email, sub, code, inv, cents, reason, period_end, trial=3):
    """invoice.paid in the newer API shape: the subscription and its metadata under parent, prices under pricing."""
    return deliver("invoice.paid", {
        "id": inv, "object": "invoice", "status": "paid", "currency": "usd", "amount_paid": cents,
        "billing_reason": reason, "customer": f"cus_{sub}",
        "parent": {"subscription_details": {"subscription": sub, "metadata": meta(email, code, trial)}},
        "lines": {"data": [{"amount": cents, "pricing": {"price_details": {"price": PRICE[code]}},
                            "period": {"end": period_end}}]}})


def ev_sub(email, sub, code, status, trial_end=None, typ="customer.subscription.updated", trial=3, period_end=None):
    return deliver(typ, {"id": sub, "object": "subscription", "status": status, "customer": f"cus_{sub}",
                         "trial_end": trial_end, "cancel_at_period_end": False,
                         "metadata": meta(email, code, trial),
                         "items": {"data": [{"price": {"id": PRICE[code]},
                                             "current_period_end": period_end or trial_end or IN_33_DAYS}]}})


def subscribe(c, code):
    """Press the plan's button; returns (response, the form sent to Stripe as a dict)."""
    n = len(posts)
    r = c.post("/app/billing/subscribe", data={"csrf": csrf(c), "plan": code})
    form = dict(posts[-1][1]) if len(posts) > n else {}
    return r, form


def set_flags(only=None, sale=None):
    for k, v in (("PLANS_ONLY", only), ("PLAN_SALE", sale)):
        if v is None:
            continue
        if v:
            os.environ[k] = "1"
        else:
            os.environ.pop(k, None)


STARTER, GROWTH, SCALE = (plans.PLANS[c] for c in ("starter", "growth", "scale"))

# ---------------------------------------------------------------- the price it is going to, and the percent off
print("\nThe percent off")
check(all(PRICE.values()), "the three plans have Stripe prices in this test setup")
check(plans.sale(STARTER) == {"next": 79, "off": 50}, "Starter: $39 next to $79 reads 50% off (50.6 rounded down)")
check(plans.sale(GROWTH) == {"next": 149, "off": 33}, "Growth: $99 next to $149 reads 33% off")
check(plans.sale(SCALE) == {"next": 349, "off": 28}, "Scale: $249 next to $349 reads 28% off")
check(all(p["usd"] == u for p, u in ((STARTER, 39), (GROWTH, 99), (SCALE, 249))), "the prices charged are unchanged")
check(plans.sale(dict(STARTER, next_usd=39)) is None and plans.sale(dict(STARTER, next_usd=None)) is None,
      "no tag once the price has caught up, or when a plan has no price to go to")
check(plans.sale(dict(STARTER, usd=40, next_usd=59)) == {"next": 59, "off": 32}, "the percent never rounds up (32.2)")
set_flags(sale=False)
check(plans.sale(STARTER) is None, "PLAN_SALE off: the price stands alone")
set_flags(sale=True)

# ---------------------------------------------------------------- the switch
print("\nThe switch")
check(plan_gate.on(), "PLANS_ONLY=1 with Stripe set up: on")
os.environ["STRIPE_WEBHOOK_SECRET"] = ""
check(not plan_gate.on(), "without a working checkout the switch stays off (nobody could pick a plan)")
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_po"
set_flags(only=False)
check(not plan_gate.on(), "default: off")
old = signup("old.free@example.com")                 # an account from before the switch
OLD_CREDITS = db.balance(ws_of("old.free@example.com"))
check(OLD_CREDITS == catalog.starter_credits() > 0, "switch off: a sign-up gets its starter credits, as before")
check(old.get("/app").status_code == 200, "switch off: straight into the app")
html = page(client(), "/pricing")
check('data-cta="pricing-free"' in html and "Start free" in html, "switch off: the public pricing still has the free tier")
check("50% off" in html and 'class="px-next"' in html, "PLAN_SALE alone: the percent off shows beside the free tier")
set_flags(sale=False)
html = page(client(), "/pricing")
check("% off" not in html and "px-next" not in html, "both off: the pricing page is as it was")
set_flags(only=True, sale=True)

# ---------------------------------------------------------------- a new sign-up
print("\nA new sign-up")
c = signup("nina@example.com")
w = ws_of("nina@example.com")
check(db.balance(w) == 0 and not q("SELECT 1 FROM ledger WHERE workspace_id=?", w["id"]),
      "no starter credits at sign-up, and nothing in the ledger")
r = c.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/plan"), "the app sends a new account to the plan step")
html = page(c, "/app/plan")
check("Pick a plan to start" in html and html.count("Start 3-day trial") == 2, "the plan step: a 3-day trial on Starter and Growth")
check("Start Scale, $249 today" in html, "Scale has no trial and says it is charged today")
check(html.count("data-plan-trial") == 2 and html.count("<b>3-day free trial.</b> Full access.") == 3 and 'fill="#BDFA25"' in html,
      "Starter and Growth carry the chip at the top: our star, 3-day free trial, full access (Scale keeps an unseen slot)")
check(re.search(r'data-plan="scale">\s*<p class="px-trial gap" aria-hidden="true">', html) is not None,
      "Scale's slot is hidden from readers and screen readers")
check("days free</b> with" not in html and "credits land with that first payment" not in html,
      "the per-card trial arithmetic is gone")
check("you carry on with the same month of credits" in html, "the step says the first payment carries on with the same credits")
check("Your card is charged when the trial ends, unless you cancel first" in html, "the step says when the card is charged")
for off, nxt in (("50% off", "$79"), ("33% off", "$149"), ("28% off", "$349")):
    check(off in html and f'<s class="px-next"><span class="sr">going to </span>{nxt}</s>' in html,
          f"the plan step shows {off} with {nxt} struck through")
check("Launch price" not in html and "launch price" not in html, "no launch-price wording")
check(c.get("/app/library").status_code == 200, "the library stays open without a plan")
html = page(c, "/app/billing")
check("No plan yet" in html and html.count("Start 3-day trial") == 2 and "Needs a plan" in html
      and html.count("data-plan-trial") == 2, "Billing stays open: no plan yet, the trial chips and buttons, and packs wait for a plan")
r = c.get("/app/create", headers={"Accept": "application/json"})
check(r.status_code == 402 and r.get_json().get("plan_required") and r.get_json()["url"].endswith("/app/plan"),
      "a JSON caller gets 402 with plan_required and the plan step's address")
with appmod.app.test_request_context("/app/x"):
    from flask import g
    g.user, g.ws = user_of("nina@example.com"), w
    try:
        core.hold_check(w)
        raised = False
    except plan_gate.PlanRequired:
        raised = True
check(raised, "anything that spends is refused until a plan is picked (core.hold_check)")
r = c.post("/app/billing/checkout", data={"csrf": csrf(c), "pack": catalog.PACKS[0]["code"]})
check(r.status_code == 302 and "#plans" in r.headers["Location"] and any("top up a plan" in m for m in flashes(c)),
      "a credit pack isn't sold without a plan")

# ---------------------------------------------------------------- the trial checkout
print("\nThe trial checkout")
r, form = subscribe(c, "starter")
check(r.status_code == 303 and r.headers["Location"].startswith("https://checkout.stripe.test/"), "Starter goes to Stripe Checkout")
check(form.get("subscription_data[trial_period_days]") == "3" and form.get("payment_method_collection") == "always",
      "the session starts a 3-day trial and always takes the card")
check(form.get("subscription_data[metadata][trial_days]") == "3" and form.get("metadata[plan]") == "starter",
      "the subscription is marked as a trial of ours")
note = form.get("custom_text[submit][message]", "")
check("3 days free with full access" in note and "$39 a month" in note and "nothing is charged" in note,
      "Stripe's page says the trial, the price after it and how to avoid the charge")
check("plan=success&trial=1&go=app" in form["success_url"] and "/app/plan?canceled=1" in form["cancel_url"],
      "afterwards the member lands in the app; a canceled checkout comes back to the plan step")
check(form.get("line_items[0][price]") == PRICE["starter"], "the price charged is Starter's own Stripe price")
r, form = subscribe(c, "scale")
check("subscription_data[trial_period_days]" not in form and form.get("payment_method_collection") == "if_required"
      and "trial=1" not in form["success_url"], "Scale: no trial in the session")
check("$249 a month" in form.get("custom_text[submit][message]", "") and "free" not in form["custom_text[submit][message]"],
      "Scale's note says the price and no trial")
html = page(c, "/app/plan?canceled=1")
check("Checkout canceled. Nothing was charged." in html, "back from a canceled checkout: nothing was charged")
refuse_note[0] = True
n = len(posts)
r, form = subscribe(c, "growth")
check(r.status_code == 303 and len(posts) == n + 2 and "custom_text[submit][message]" not in form
      and form.get("subscription_data[trial_period_days]") == "3",
      "if Stripe refuses the note the session is made again without it, trial intact")
refuse_note[0] = False

# ---------------------------------------------------------------- the trial starts
print("\nThe trial starts (checkout, then its $0 invoice, then the subscription)")
ev_checkout("nina@example.com", "sub_nina", "starter")
w = ws_of("nina@example.com")
check(w["plan_status"] == "pending" and c.get("/app").status_code == 200, "checkout done: confirming, and the app is open")
out = ev_invoice("nina@example.com", "sub_nina", "starter", "in_nina_0", 0, "subscription_create", IN_3_DAYS).get_json()
w = ws_of("nina@example.com")
check(out.get("trial") and out["credited"] == 2000, "the $0 invoice starts the trial with the plan's full 2,000 credits")
check(w["plan"] == "starter" and w["plan_status"] == "trialing" and plans.plan_code(w) == "starter", "the workspace is on Starter, trialing")
check(w["plan_credits"] == 2000 and w["credits"] == 0 and w["plan_granted"] == 2000 and w["trial_open"] == 1,
      "2,000 plan credits, none of the pack kind, and the trial is open")
check(w["trial_ends_at"] and w["plan_paid_through"] == w["trial_ends_at"], "the trial's end is on the workspace")
t = q("SELECT * FROM plan_trials WHERE workspace_id=?", w["id"])
check(len(t) == 1 and t[0]["credits"] == 2000 and t[0]["subscription_id"] == "sub_nina" and t[0]["email_norm"] == "nina@example.com"
      and t[0]["fingerprint"] == "fp_sub_nina", "the trial is recorded once, with the person's email and the card's fingerprint")
inv = q("SELECT * FROM plan_invoices WHERE invoice_id='in_nina_0'")[0]
check(inv["billing_reason"] == "trial" and inv["credits"] == 2000 and inv["amount_cents"] == 0, "the $0 invoice is filed as the trial")
ev_sub("nina@example.com", "sub_nina", "starter", "trialing", IN_3_DAYS, typ="customer.subscription.created")
ev_invoice("nina@example.com", "sub_nina", "starter", "in_nina_0", 0, "subscription_create", IN_3_DAYS)
w = ws_of("nina@example.com")
check(w["plan_credits"] == 2000 and len(q("SELECT 1 FROM plan_trials WHERE workspace_id=?", w["id"])) == 1,
      "the subscription event and a repeat of the invoice grant nothing more")
html = page(c, "/app/billing")
check('id="plan-trial"' in html and "Free trial, ends" in html and "$39 is charged then, and you carry on with this month" in html
      and "came without credits" not in html, "Billing says when the trial ends, what is charged and that the credits carry on")
check(c.get("/app").status_code == 200 and c.get("/app/plan").status_code == 302, "in the trial the app is open and the plan step is gone")
check("Needs a plan" not in html, "packs can be bought on a plan")
with db.connect() as conn:
    check(plan_gate.trial_days(conn, w, user_of("nina@example.com"), "growth") == 0, "no second trial for this workspace")

# ---------------------------------------------------------------- the first payment
print("\nThe first payment")
with db.connect() as conn:
    db.adjust_credits(conn, w["id"], -50, "generation")
    conn.commit()
out = ev_invoice("nina@example.com", "sub_nina", "starter", "in_nina_1", 3900, "subscription_cycle", IN_33_DAYS).get_json()
w = ws_of("nina@example.com")
check(out["credited"] == 0 and w["plan_credits"] == 1950, "the first payment resets nothing: the 1,950 left carry on")
check(w["plan_status"] == "active" and w["trial_open"] == 0 and w["plan_granted"] == 2000, "the plan is active and the trial is closed")
check(q("SELECT reason FROM ledger WHERE workspace_id=? ORDER BY id DESC LIMIT 1", w["id"])[0]["reason"] == "generation"
      and q("SELECT credits, amount_cents FROM plan_invoices WHERE invoice_id='in_nina_1'")[0]["amount_cents"] == 3900,
      "nothing new in the ledger; the payment itself is on file")
out = ev_invoice("nina@example.com", "sub_nina", "starter", "in_nina_2", 3900, "subscription_cycle", IN_63_DAYS).get_json()
w = ws_of("nina@example.com")
check(out["credited"] == 2000 and w["plan_credits"] == 2000, "the renewal after that refills to the full 2,000, replacing what was left")
ev_invoice("nina@example.com", "sub_nina", "starter", "in_nina_0b", 0, "subscription_create", IN_3_DAYS)
w = ws_of("nina@example.com")
check(w["plan_status"] == "active" and w["plan_credits"] == 2000, "a stray trial invoice later changes nothing")

# ---------------------------------------------------------------- events in another order
print("\nThe subscription event first")
c2 = signup("omar@example.com")
ev_sub("omar@example.com", "sub_omar", "growth", "trialing", IN_3_DAYS, typ="customer.subscription.created")
w = ws_of("omar@example.com")
check(w["plan"] == "growth" and w["plan_status"] == "trialing" and w["plan_credits"] == 6000 and w["trial_open"] == 1,
      "Growth's trial starts from the subscription event alone, with its full 6,000 credits")
out = ev_invoice("omar@example.com", "sub_omar", "growth", "in_omar_0", 0, "subscription_create", IN_3_DAYS).get_json()
w = ws_of("omar@example.com")
check(out["credited"] == 0 and w["plan_credits"] == 6000 and w["plan_status"] == "trialing", "its $0 invoice afterwards grants nothing more")
out = ev_invoice("omar@example.com", "sub_omar", "growth", "in_omar_1", 9900, "subscription_cycle", IN_33_DAYS).get_json()
check(out["credited"] == 0 and ws_of("omar@example.com")["plan_credits"] == 6000, "Growth's first payment adds nothing and takes nothing")

print("\nA plan change during the trial")
signup("wes@example.com")
ev_invoice("wes@example.com", "sub_wes", "starter", "in_wes_0", 0, "subscription_create", IN_3_DAYS)
out = ev_invoice("wes@example.com", "sub_wes", "growth", "in_wes_up", 0, "subscription_update", IN_3_DAYS).get_json()
w = ws_of("wes@example.com")
check(out["credited"] == 0 and w["plan_credits"] == 2000 and w["plan_status"] == "trialing" and w["trial_open"] == 1,
      "upgrading in the trial (a $0 invoice) lands no month's credits: nothing was paid")
ev_sub("wes@example.com", "sub_wes", "growth", "trialing", IN_3_DAYS)
w = ws_of("wes@example.com")
check(w["plan"] == "growth" and w["plan_credits"] == 2000, "the subscription event moves the plan; the trial's credits stay as they were")
out = ev_invoice("wes@example.com", "sub_wes", "growth", "in_wes_1", 9900, "subscription_cycle", IN_33_DAYS).get_json()
w = ws_of("wes@example.com")
check(out["credited"] == 4000 and w["plan_credits"] == 6000 and w["plan_status"] == "active",
      "Growth's first payment adds the difference between the two plans: 6,000 in all")

print("\nThe first payment before word of the trial")
signup("pia@example.com")
ev_invoice("pia@example.com", "sub_pia", "starter", "in_pia_1", 3900, "subscription_cycle", IN_33_DAYS)
w = ws_of("pia@example.com")
check(w["plan_status"] == "active" and w["plan_credits"] == 2000, "paid first: the full month")
ev_invoice("pia@example.com", "sub_pia", "starter", "in_pia_0", 0, "subscription_create", IN_3_DAYS)
ev_sub("pia@example.com", "sub_pia", "starter", "trialing", IN_3_DAYS)
w = ws_of("pia@example.com")
check(w["plan_credits"] == 2000 and w["trial_open"] == 0 and q("SELECT credits FROM plan_trials WHERE workspace_id=?", w["id"])[0]["credits"] == 0,
      "the trial arriving late is recorded and takes nothing away")

# ---------------------------------------------------------------- Scale, no trial
print("\nScale")
c3 = signup("rosa@example.com")
with db.connect() as conn:
    check(plan_gate.trial_days(conn, ws_of("rosa@example.com"), user_of("rosa@example.com"), "scale") == 0, "Scale never has a trial")
ev_checkout("rosa@example.com", "sub_rosa", "scale", trial=0)
out = ev_invoice("rosa@example.com", "sub_rosa", "scale", "in_rosa_1", 24900, "subscription_create", IN_33_DAYS, trial=0).get_json()
w = ws_of("rosa@example.com")
check(out["credited"] == 18000 and w["plan_status"] == "active" and w["plan_credits"] == 18000 and w["trial_open"] == 0
      and not q("SELECT 1 FROM plan_trials WHERE workspace_id=?", w["id"]), "Scale is charged at once and gets the full month")
check("trial" not in out, "its first invoice is not a trial")

# ---------------------------------------------------------------- a 100% code is not a trial
signup("sam@example.com")
out = deliver("invoice.paid", {
    "id": "in_sam_1", "status": "paid", "currency": "usd", "amount_paid": 0, "billing_reason": "subscription_create",
    "customer": "cus_sam", "parent": {"subscription_details": {"subscription": "sub_sam", "metadata": meta("sam@example.com", "starter", 0)}},
    "lines": {"data": [{"amount": 3900, "pricing": {"price_details": {"price": PRICE["starter"]}}, "period": {"end": IN_33_DAYS}}]}}).get_json()
check(out["credited"] == 2000 and "trial" not in out and ws_of("sam@example.com")["plan_status"] == "active",
      "a plan made free by a 100% code (no trial mark) still gets its full month")

# ---------------------------------------------------------------- one trial per person
print("\nOne trial per person")
c4 = signup("n.i.n.a+two@example.com".replace("example.com", "gmail.com"))
run_sql("UPDATE plan_trials SET email_norm='nina@gmail.com' WHERE email_norm='nina@example.com'")
with db.connect() as conn:
    again = plan_gate.trial_days(conn, ws_of("n.i.n.a+two@gmail.com"), user_of("n.i.n.a+two@gmail.com"), "starter")
check(again == 0, "the same person on a new account (dots and +tag folded) gets no second trial")
html = page(c4, "/app/plan")
check("Start 3-day trial" not in html and "Start Starter, $39 today" in html and "Monthly, and you can cancel any time." in html,
      "their plan step offers the plans without a trial")
r, form = subscribe(c4, "starter")
check("subscription_data[trial_period_days]" not in form and "trial_days" not in str(form) and "go=app" in form["success_url"],
      "and their checkout has no trial")

# ---------------------------------------------------------------- one trial per card
print("\nOne trial per card")
cards.update(sub_zed1="fp_shared", sub_zed2="fp_shared", sub_yan="fp_old_free")
signup("zed1@example.com")
ev_invoice("zed1@example.com", "sub_zed1", "starter", "in_zed1_0", 0, "subscription_create", IN_3_DAYS)
check(ws_of("zed1@example.com")["plan_credits"] == 2000, "a card's first trial has full access")
cz = signup("zed2@example.com")
out = ev_invoice("zed2@example.com", "sub_zed2", "starter", "in_zed2_0", 0, "subscription_create", IN_3_DAYS).get_json()
w = ws_of("zed2@example.com")
check(out["credited"] == 0 and w["plan_credits"] == 0 and w["plan_status"] == "trialing" and w["trial_open"] == 1
      and q("SELECT credits FROM plan_trials WHERE workspace_id=?", w["id"])[0]["credits"] == 0,
      "the same card on a second account: Stripe's trial runs, without trial credits")
check("came without credits; they land with the first payment" in page(cz, "/app/billing"), "and Billing says why")
out = ev_invoice("zed2@example.com", "sub_zed2", "starter", "in_zed2_1", 3900, "subscription_cycle", IN_33_DAYS).get_json()
check(out["credited"] == 2000 and ws_of("zed2@example.com")["plan_credits"] == 2000, "its credits land with the first payment")
signup("yan@example.com")
run_sql("INSERT INTO card_trials (workspace_id, fingerprint, email_norm, trial, credits, created_at) VALUES (?,?,?,?,?,?)",
        ws_of("old.free@example.com")["id"], "fp_old_free", None, 1, 40, db.now())
out = ev_sub("yan@example.com", "sub_yan", "growth", "trialing", IN_3_DAYS, typ="customer.subscription.created").get_json()
check(ws_of("yan@example.com")["plan_credits"] == 0, "a card that already earned the old free credits elsewhere gets no trial credits either")
_real_fp = billing.subscription_card_fingerprint
billing.subscription_card_fingerprint = lambda sub: (_ for _ in ()).throw(billing.BillingError("Stripe unreachable"))
signup("xia@example.com")
ev_invoice("xia@example.com", "sub_xia", "starter", "in_xia_0", 0, "subscription_create", IN_3_DAYS)
check(ws_of("xia@example.com")["plan_credits"] == 2000, "if the card can't be read the trial goes ahead")
billing.subscription_card_fingerprint = _real_fp

# ---------------------------------------------------------------- a trial that is canceled
print("\nA trial that ends without paying")
c5 = signup("tess@example.com")
ev_invoice("tess@example.com", "sub_tess", "starter", "in_tess_0", 0, "subscription_create", IN_3_DAYS)
ev_sub("tess@example.com", "sub_tess", "starter", "canceled", IN_3_DAYS, typ="customer.subscription.deleted")
w = ws_of("tess@example.com")
check(w["plan_status"] == "canceled" and plans.plan_code(w) == "starter" and w["plan_credits"] == 2000 and w["trial_open"] == 0,
      "canceled during the trial: the plan and its trial credits stay to the trial's end")
run_sql("UPDATE workspaces SET plan_paid_through='2020-01-01T00:00:00+00:00' WHERE id=?", w["id"])
with db.connect() as conn:
    core.settle_plan_lapse(conn, w["id"])
w = ws_of("tess@example.com")
check(w["plan"] == "free" and w["plan_credits"] == 0, "after it: no plan, and the trial credits are gone")
r = c5.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/plan"), "the app asks for a plan again")
check("Start 3-day trial" not in page(c5, "/app/plan"), "with no second trial")
signup("uma@example.com")
ev_invoice("uma@example.com", "sub_uma", "starter", "in_uma_0", 0, "subscription_create", NOW - 60)
ev_sub("uma@example.com", "sub_uma", "starter", "past_due", NOW - 60)
w = ws_of("uma@example.com")
with db.connect() as conn:
    check(plans.plan_code(w) == "free" and plans.renewal_paused(w) and not plan_gate.needs_plan(conn, w, user_of("uma@example.com")),
          "a card that fails at the trial's end pauses the plan; that is fixed on Billing, not with a new checkout")

# ---------------------------------------------------------------- back from Stripe ahead of the webhook
print("\nBack from Stripe before its webhook")
c6 = signup("vic@example.com")
w = ws_of("vic@example.com")
sessions["cs_vic"] = {"id": "cs_vic", "object": "checkout.session", "mode": "subscription", "status": "complete",
                      "payment_status": "no_payment_required", "customer": "cus_vic", "subscription": "sub_vic",
                      "metadata": meta("vic@example.com", "starter")}
sessions["cs_other"] = dict(sessions["cs_vic"], id="cs_other", metadata=meta("nina@example.com", "starter"))
r = c6.get("/app/billing?plan=success&trial=1&go=app&session_id=cs_other")
check(ws_of("vic@example.com")["plan_status"] is None, "someone else's session id does nothing")
r = c6.get("/app/billing?plan=success&trial=1&go=app&session_id=cs_vic")
w = ws_of("vic@example.com")
check(r.status_code == 302 and r.headers["Location"].endswith("/app") and w["plan_status"] == "pending"
      and w["stripe_subscription_id"] == "sub_vic", "their own finished session is applied and they go on to the app")
check(any("3-day trial has started" in m for m in flashes(c6)), "with a note that the trial has started")
check(c6.get("/app").status_code == 200, "and the app is open while Stripe confirms")

# ---------------------------------------------------------------- accounts from before
print("\nAccounts from before the switch")
r = old.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/plan"), "a free account is asked to pick a plan at its next visit")
html = page(old, "/app/plan")
check(html.count("Start 3-day trial") == 2, "and can start with the trial")
check(old.get("/app/library").status_code == 200 and old.get("/app/billing").status_code == 200, "its library and Billing stay open")
w = ws_of("old.free@example.com")
check(db.balance(w) == OLD_CREDITS, "its credits are untouched")
run_sql("INSERT INTO jobs (workspace_id, user_id, tool, template_id, brief, params_json, status, credits, cost_usd, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", w["id"], user_of("old.free@example.com")["id"], "ad_image", "t", "", "{}", "done", 0, 0, db.now(), db.now())
html = page(old, "/app/plan")
check("Pick a plan to keep making ads" in html and "Your ads, products and credits are all still here." in html
      and "Go to your library" in html, "an account that has made ads is told they are all still there")
run_sql("INSERT INTO purchases (workspace_id, user_id, pack, credits, amount_cents, currency, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        w["id"], user_of("old.free@example.com")["id"], "starter", 1500, 2900, "usd", "paid", db.now())
check(old.get("/app").status_code == 200, "an account that paid for a pack and still holds credits keeps making ads")
run_sql("UPDATE workspaces SET credits=0 WHERE id=?", w["id"])
check(old.get("/app").status_code == 302, "once those credits are used it picks a plan")
run_sql("UPDATE workspaces SET plan_override='scale' WHERE id=?", w["id"])
check(old.get("/app").status_code == 200, "a comped workspace is never asked")
with db.connect() as conn:
    check(plan_gate.trial_days(conn, ws_of("old.free@example.com"), user_of("old.free@example.com"), "starter") == 0,
          "and a workspace already on a plan or comp starts no trial")
run_sql("UPDATE workspaces SET plan_override=NULL WHERE id=?", w["id"])
run_sql("UPDATE users SET is_admin=1 WHERE email='old.free@example.com'")
check(old.get("/app").status_code == 200, "a site admin is never asked")
run_sql("UPDATE users SET is_admin=0 WHERE email='old.free@example.com'")

# ---------------------------------------------------------------- the public pages
print("\nThe public pages")
pub = client()
html = page(pub, "/pricing")
check('data-cta="pricing-free"' not in html and "Start free" not in html, "pricing: no free tier and no Start free")
check(html.count("% off") >= 3 and all(x in html for x in ("50% off", "33% off", "28% off")), "pricing: the percent off on each plan")
check(all(f'<s class="px-next"><span class="sr">going to </span>{x}</s>' in html for x in ("$79", "$149", "$349")),
      "pricing: the price each plan is going to, struck through")
check("Try Starter free for 3 days" in html and "Try Growth free for 3 days" in html and "Start Scale, $249/mo" in html,
      "pricing: trial buttons on Starter and Growth, a plain one on Scale")
check(html.count("data-plan-trial") == 2 and html.count("<b>3-day free trial.</b> Full access.") == 3 and 'fill="#BDFA25"' in html,
      "pricing: the star chip at the top of Starter and Growth, 3-day free trial, full access")
check("days free</b> with" not in html and "200 credits" not in html and "Charged at once. No trial." not in html,
      "pricing: the per-card trial wording is gone")
check("Your card is charged the plan's price when the trial ends unless you cancel first" in html
      and "Scale has no trial and is charged at once" in html, "pricing: the trial's terms are stated once, under the plans")
check("No trial that bills you" not in html and "on any plan or none" not in html, "pricing: the lines about free sign-up are gone")
check("Launch price" not in html and "countdown" not in html.lower() and "ends in" not in html.lower(), "pricing: no launch wording and no timer")
html = page(pub, "/")
check("Start free" not in html and "Try it free for 3 days" in html, "home: the buttons offer the 3-day trial")
html = page(pub, "/signup")
check("Create your account" in html and "Next, you pick a plan" in html and "free credits" not in html, "sign-up says a plan comes next")
txt = page(pub, "/llms.txt")
check("Every account is on a plan" in txt and "free starter credits" not in txt and "the free tier" not in txt, "llms.txt follows the switch")
faq = dict(public_site.home_faq())
check(faq["Do I need a subscription?"].startswith("Yes: every account is on a plan."), "the FAQ answers the subscription question truthfully")
BAD = re.compile(r"free credits|starter credits|free sign-up|nothing charged|nothing is charged at sign-up|no trial that|free studio|"
                 r"free image ads|start free|only when you want|no subscription needed|charges nothing|free lets|no end date|"
                 r"\{[a-z_]+\}", re.I)
left = []
for slug in public_site.SOLUTIONS:
    left += [(slug, m.group(0)) for m in BAD.finditer(json.dumps([public_site.solution(slug), public_site.meta("solution", {"slug": slug})["description"]]))]
for slug in public_site.GUIDES:
    left += [(slug, m.group(0)) for m in BAD.finditer(json.dumps([public_site.guide(slug), public_site.meta("guide", {"slug": slug})["description"]]))]
for ep in public_site.PAGES:
    left += [(ep, m.group(0)) for m in BAD.finditer(public_site.meta(ep, {})["description"])]
left += [("faq", m.group(0)) for m in BAD.finditer(json.dumps(public_site.home_faq()))]
check(not left, f"no public page still promises a free start or an unbilled trial {left[:4]}")
check([o["name"] for o in public_site.offers()] == ["Starter plan", "Growth plan", "Scale plan"], "the structured data lists the paid plans only")
check(public_site.solution("makeugc-alternative")["title"] == "MakeUGC Alternative: Every Ad Priced First · Campaignify",
      "the comparison page built on \"no trial that bills you\" has a new title")
set_flags(only=False, sale=False)
check(public_site.solution("makeugc-alternative")["title"] == "MakeUGC Alternative: No Trial That Bills You · Campaignify"
      and dict(public_site.home_faq())["Do I need a subscription?"].startswith("No. Start with your free credits")
      and len(public_site.offers()) == 4, "switch off: the copy is back to free sign-up")
html = page(client(), "/")
check("Try it free for 3 days" not in html, "switch off: the home page is as it was")
set_flags(only=True, sale=True)

print(f"\n{passed} plans-only checks passed")

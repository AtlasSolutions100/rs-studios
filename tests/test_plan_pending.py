"""Plans switch on only when paid (BILL-3), a paid period is kept to its end,
and a failed renewal pauses the workspace without taking its credits
(observation 8). A throwaway database; Stripe faked.

    python tests/test_plan_pending.py      (tests/test_app.py runs it too)
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_pending_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")  # sign-ups here go straight in; test_login.py checks the emailed-link step
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0",
                  RS_EMAIL_VERIFY="0", STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")

import app as appmod  # noqa: E402
import billing  # noqa: E402
import billing_holds  # noqa: E402
import db  # noqa: E402
import plans  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def no_http(*a, **kw):
    raise AssertionError("no Stripe calls in these tests")


class _Empty:
    status_code = 200

    def json(self):
        return {"object": "list", "data": []}


def only_invoice_payments(url, params=None, **kw):
    # invoice.paid without a PaymentIntent reads it from /v1/invoice_payments (N1): none here
    if url.endswith("/invoice_payments"):
        return _Empty()
    return no_http()


billing.requests.post = no_http
billing.requests.get = only_invoice_payments
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.7.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    with db.connect() as conn:
        wid = conn.execute("SELECT workspace_id FROM users WHERE email=?", (email,)).fetchone()[0]
    return c, wid


def ws(wid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()


_n = [0]


def deliver(typ, obj):
    _n[0] += 1
    body = json.dumps({"id": f"evt_p{_n[0]}", "type": typ, "data": {"object": obj}}).encode()
    return client().post("/webhooks/stripe", data=body, content_type="application/json",
                         headers={"Stripe-Signature": billing.sign(body, "whsec_x")})


MONTH_AHEAD = int(time.time()) + 30 * 86400
MONTH_AGO = int(time.time()) - 86400


def meta(wid, code="growth"):
    return {"app": "campaignify", "kind": "plan", "workspace_id": str(wid), "plan": code}


def checkout(wid, sub, code="growth"):
    return deliver("checkout.session.completed", {
        "id": f"cs_{sub}", "object": "checkout.session", "mode": "subscription", "customer": f"cus_{sub}",
        "subscription": sub, "payment_status": "unpaid", "metadata": meta(wid, code)})


def invoice(wid, sub, inv_id, code="growth", reason="subscription_create", end=MONTH_AHEAD):
    p = plans.PLANS[code]
    return deliver("invoice.paid", {
        "id": inv_id, "object": "invoice", "status": "paid", "currency": "usd", "customer": f"cus_{sub}",
        "billing_reason": reason, "amount_paid": p["usd"] * 100, "subscription": sub,
        "subscription_details": {"metadata": meta(wid, code)},
        "lines": {"data": [{"amount": p["usd"] * 100, "price": {"id": p["price_id"]}, "period": {"end": end}}]}})


def sub_event(wid, sub, status, code="growth", deleted=False, end=MONTH_AHEAD):
    return deliver("customer.subscription.deleted" if deleted else "customer.subscription.updated", {
        "id": sub, "object": "subscription", "status": status, "customer": f"cus_{sub}", "cancel_at_period_end": False,
        "metadata": meta(wid, code),
        "items": {"data": [{"price": {"id": plans.PLANS[code]["price_id"]}, "current_period_end": end}]}})


print("== checkout first, then the invoice")
a, A = signup("ann@example.com")
pack0 = ws(A)["credits"]
r = checkout(A, "sub_a")
w = ws(A)
check(r.status_code == 200 and w["plan_status"] == "pending" and w["plan_pending"] == "growth"
      and plans.plan_code(w) == "free" and not plans.has(w, "copy"), "checkout done: pending, no plan features yet")
html = a.get("/app/billing").get_data(as_text=True)
check("Confirming payment for Growth." in html, "the billing page says Confirming payment")
r = a.post("/app/billing/subscribe", data={"csrf": csrf(a), "plan": "scale"})
check(r.status_code == 302 and "/app/billing" in r.headers["Location"], "no second checkout while one is confirming")
check(deliver("customer.subscription.created", {"id": "sub_a", "object": "subscription", "status": "incomplete",
                                                "metadata": meta(A)}).get_json().get("ignored")
      and ws(A)["plan_status"] == "pending", "an incomplete subscription switches nothing on")
r = invoice(A, "sub_a", "in_a1")
w = ws(A)
check(r.get_json()["credited"] == 6000 and w["plan_status"] == "active" and plans.plan_code(w) == "growth"
      and w["plan_pending"] is None and w["plan_paid_through"], "invoice.paid: Growth on, 6,000 credits, paid through stored")
check("Confirming payment" not in a.get("/app/billing").get_data(as_text=True), "the note is gone")

print("== the invoice first, then the checkout")
b, B = signup("bo@example.com")
invoice(B, "sub_b", "in_b1")
checkout(B, "sub_b")
w = ws(B)
check(w["plan_status"] == "active" and plans.plan_code(w) == "growth" and w["plan_pending"] is None,
      "a late checkout event doesn't put a paid plan back to pending")

print("== never paid")
c, C = signup("cy@example.com")
checkout(C, "sub_c")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_credits=500 WHERE id=?", (C,))   # e.g. a free-month code's credits
    conn.commit()
sub_event(C, "sub_c", "incomplete_expired")
w = ws(C)
check(w["plan_status"] == "incomplete_expired" and plans.plan_code(w) == "free" and w["plan_credits"] == 500
      and w["credits"] == ws(C)["credits"], "incomplete_expired: back to free, no credits touched")
d, D = signup("di@example.com")
checkout(D, "sub_d")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_pending_at='2020-01-01T00:00:00+00:00' WHERE id=?", (D,))
    conn.commit()
appmod._settle_plans()
check(ws(D)["plan_status"] == "incomplete_expired" and ws(D)["plan_pending"] is None,
      "still confirming after 24 hours: the hourly sync sets it back")
e, E = signup("ed@example.com")
checkout(E, "sub_e_new")
check(sub_event(E, "sub_e_old", "canceled", deleted=True).get_json().get("ignored") and ws(E)["plan_status"] == "pending",
      "an older subscription ending doesn't touch a checkout being confirmed")

print("== a paid period is kept to its end")
sub_event(A, "sub_a", "unpaid")
w = ws(A)
check(plans.plan_code(w) == "growth" and w["plan_credits"] == 6000 and not plans.renewal_paused(w),
      "unpaid while the paid month runs: plan and credits stay")
sub_event(A, "sub_a", "canceled", deleted=True)
w = ws(A)
check(plans.plan_code(w) == "growth" and w["plan_credits"] == 6000, "canceled mid-month: kept to the end of the month")
check("ends " in a.get("/app/billing").get_data(as_text=True), "billing says when it ends")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_paid_through='2020-01-01T00:00:00+00:00' WHERE id=?", (A,))
    conn.commit()
a.get("/app/library")
w = ws(A)
check(plans.plan_code(w) == "free" and w["plan"] == "free" and w["plan_credits"] == 0 and w["credits"] == pack0,
      "the month ends: free, unused plan credits expire, pack credits kept")

print("== a failed renewal pauses, keeps the credits")
f, F = signup("fa@example.com")
invoice(F, "sub_f", "in_f1", end=MONTH_AGO)      # the month paid for is over
sub_event(F, "sub_f", "past_due", end=MONTH_AHEAD)
w = ws(F)
check(w["plan_credits"] == 6000 and plans.renewal_paused(w) and plans.plan_code(w) == "free",
      "past_due after the paid month: paused, the 6,000 plan credits kept")
r = f.post("/app/generate", data={"csrf": csrf(f), "tool": "ad_image", "brief": "Serum on a vanity", "batch": "1"},
           headers={"Accept": "application/json"})
check(r.status_code == 402 and r.get_json()["reason"] == "renewal"
      and r.get_json()["error"] == billing_holds.RENEWAL_MESSAGE, "generation refused: 402, renewal")
html = f.get("/app/billing").get_data(as_text=True)
check(billing_holds.RENEWAL_MESSAGE.replace("'", "&#39;") in html and "Growth plan paused." in html,
      "banner and billing say what happened and what to do")
sub_event(F, "sub_f", "unpaid", end=MONTH_AHEAD)
check(ws(F)["plan_credits"] == 6000 and plans.renewal_paused(ws(F)), "unpaid: still paused, credits still kept")

print("== N5: a paused renewal is fixed in the portal, never with a second checkout")
portals, checkouts = [], []
_portal, _subsess = billing.create_portal_session, billing.create_subscription_session
billing.create_portal_session = lambda **kw: portals.append(kw) or "https://billing.stripe.example/p/session"
billing.create_subscription_session = lambda **kw: checkouts.append(kw) or {"id": "cs_x", "url": "https://checkout.example"}
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET stripe_customer_id='cus_sub_f' WHERE id=?", (F,))
    conn.commit()
html = f.get("/app/billing").get_data(as_text=True)
check("Get Growth" not in html and "Get Starter" not in html and "Update your card" in html
      and "Update your card first" in html, "Billing offers no plan checkout, only Update your card")
r = f.post("/app/billing/subscribe", data={"csrf": csrf(f), "plan": "scale"})
check(r.status_code == 303 and r.headers["Location"].startswith("https://billing.stripe.example/") and portals
      and not checkouts, "Subscribe sends a paused workspace to the Stripe billing portal, not to a checkout")
alerts_mod = __import__("alerts")
alerts_mod.ASYNC = False
r = checkout(F, "sub_f_second")       # a checkout opened before the pause finishes anyway
w = ws(F)
with db.connect() as conn:
    second = conn.execute("SELECT 1 FROM meta WHERE key=?", (alerts_mod.ALERT_META + f"second_subscription:{F}",)).fetchone()
check(r.status_code == 200 and w["plan_status"] == "unpaid" and w["stripe_subscription_id"] == "sub_f"
      and w["plan_pending"] is None and plans.renewal_paused(w) and second,
      "a checkout that completes anyway: no pending, the pause stays, the operator is told about the second subscription")
r = f.post("/app/generate", data={"csrf": csrf(f), "tool": "ad_image", "brief": "Serum on a vanity", "batch": "1"},
           headers={"Accept": "application/json"})
check(r.status_code == 402 and r.get_json()["reason"] == "renewal", "and generation is still refused")
billing.create_portal_session, billing.create_subscription_session = _portal, _subsess
invoice(F, "sub_f", "in_f2", reason="subscription_cycle")
w = ws(F)
check(w["plan_status"] == "active" and not plans.renewal_paused(w) and w["plan_credits"] == 6000
      and plans.plan_code(w) == "growth", "the card works again: the new month lands, back to normal")
g_, G = signup("gi@example.com")
invoice(G, "sub_g", "in_g1", end=MONTH_AGO)
sub_event(G, "sub_g", "unpaid")
sub_event(G, "sub_g", "canceled", deleted=True)
w = ws(G)
check(plans.plan_code(w) == "free" and w["plan_credits"] == 0 and not plans.renewal_paused(w),
      "never paid, then canceled: free, plan credits expire, the pause ends")

print("== the switches")
os.environ["RENEWAL_PAUSE"] = "off"
h, H = signup("hu@example.com")
invoice(H, "sub_h", "in_h1", end=MONTH_AGO)
sub_event(H, "sub_h", "past_due")
check(plans.plan_code(ws(H)) == "growth" and not plans.renewal_paused(ws(H)), "RENEWAL_PAUSE=off: past_due keeps the plan")
sub_event(H, "sub_h", "unpaid")
check(plans.plan_code(ws(H)) == "free" and ws(H)["plan_credits"] == 0, "RENEWAL_PAUSE=off: unpaid ends it, as before")
os.environ.pop("RENEWAL_PAUSE")
os.environ["PLAN_PENDING"] = "off"
i, I = signup("ia@example.com")
checkout(I, "sub_i")
check(ws(I)["plan_status"] == "active" and ws(I)["plan"] == "growth", "PLAN_PENDING=off: checkout switches it on, as before")
os.environ.pop("PLAN_PENDING")

print(f"\nall {passed} checks passed ({TMP})")

"""Refunds and disputes (billing_holds.py): credits voided, the workspace paused
(downloads only), dispute won restores, Stripe event ids deduplicated. A
throwaway database; Stripe faked.

    python tests/test_billing_holds.py      (tests/test_app.py runs it too)
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_holds_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0",
                  RS_EMAIL_VERIFY="0", STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")

import app as appmod  # noqa: E402
import billing  # noqa: E402
import billing_holds  # noqa: E402
import db  # noqa: E402
import plans  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, str(body)

    def json(self):
        return self.body


INVOICE_PAYMENTS = {}   # payment intent -> invoice id (Stripe's /v1/invoice_payments)
gets = []


def fake_get(url, params=None, auth=None, timeout=0):
    path = url.split("/v1", 1)[1]
    gets.append((path, params))
    if path == "/invoice_payments":
        pi = dict(params).get("payment[payment_intent]")
        inv = INVOICE_PAYMENTS.get(pi)
        return Resp({"object": "list", "data": [{"object": "invoice_payment", "invoice": inv}] if inv else []})
    raise AssertionError(f"unexpected Stripe GET {path}")


def no_post(*a, **kw):
    raise AssertionError("no Stripe writes in these tests")


billing.requests.get, billing.requests.post = fake_get, no_post
appmod._maybe_reconcile = lambda ws_id, force=False: 0

_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    return c


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                            (email,)).fetchone()


def ws(wid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()


_n = [0]


def deliver(typ, obj, eid=None):
    _n[0] += 1
    body = json.dumps({"id": eid or f"evt_{_n[0]}", "type": typ, "data": {"object": obj}}).encode()
    return client().post("/webhooks/stripe", data=body, content_type="application/json",
                         headers={"Stripe-Signature": billing.sign(body, "whsec_x")})


def buy_pack(wid, pi, pack="starter", credits=2900, cents=2900, deliver_it=True):
    with db.connect() as conn:
        pid = conn.execute("INSERT INTO purchases (workspace_id, pack, credits, amount_cents, currency, status, "
                           "stripe_session_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (wid, pack, credits, cents, "usd", "open", f"cs_{pi}", db.now())).lastrowid
        conn.commit()
    sess = {"id": f"cs_{pi}", "object": "checkout.session", "mode": "payment", "amount_total": cents, "currency": "usd",
            "payment_status": "paid", "payment_intent": pi,
            "metadata": {"app": "campaignify", "purchase_id": str(pid), "workspace_id": str(wid), "pack": pack}}
    return deliver("checkout.session.completed", sess) if deliver_it else sess


def charge(pi, refunded, amount=2900, cid=None, invoice=None):
    c = {"id": cid or f"ch_{pi}", "object": "charge", "payment_intent": pi, "amount": amount,
         "amount_refunded": refunded, "refunded": refunded >= amount, "metadata": {}}
    if invoice:
        c["invoice"] = invoice
    return c


def dispute(pi, did, status="needs_response", amount=2900):
    return {"id": did, "object": "dispute", "charge": f"ch_{pi}", "payment_intent": pi, "amount": amount,
            "status": status, "reason": "fraudulent"}


def spend(wid, n):
    with db.connect() as conn:
        ok = db.adjust_credits(conn, wid, -n, "test spend")
        conn.commit()
    return ok


print("== a refunded pack: credits voided, workspace paused")
a = signup("ann@example.com")
A = ws_of("ann@example.com")
start = A["credits"]
r = buy_pack(A["id"], "pi_a")
check(r.status_code == 200 and ws(A["id"])["credits"] == start + 2900, "the pack is credited")
check(spend(A["id"], start + 2500), "the member spends most of it")
r = deliver("charge.refunded", charge("pi_a", 2900), eid="evt_refund_a")
w = ws(A["id"])
check(r.status_code == 200 and r.get_json()["voided"] == 2900 and w["credits"] == -2500,
      "full refund voids the pack's 2,900 credits; the balance goes below zero (they were spent)")
check(w["billing_hold"] == 1 and "Refund on Starter pack" in (w["billing_hold_reason"] or ""), "the workspace is on hold, with why")
with db.connect() as conn:
    led = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC", (A["id"],)).fetchone()
check(led["delta"] == -2900 and led["reason"] == "Payment refunded: Starter pack credits removed", "the ledger says why")
r = deliver("charge.refunded", charge("pi_a", 2900), eid="evt_refund_a")
check(r.get_json().get("duplicate") and ws(A["id"])["credits"] == -2500, "a replayed event id is a no-op")
r = deliver("charge.refunded", charge("pi_a", 2900))
check(r.get_json().get("voided") == 0 and ws(A["id"])["credits"] == -2500,
      "the same refund in a new event voids nothing more")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM stripe_events WHERE id='evt_refund_a'").fetchone()[0] == 1,
          "stripe_events keeps the event id")

print("== on hold: downloads only")
html = a.get("/app/library").get_data(as_text=True)
check("A payment was reversed. Email us to restore access." in html and "mailto:hello@campaignify.ai" in html,
      "every page shows the banner; the library still opens")
r = a.post("/app/generate", data={"csrf": csrf(a), "tool": "ad_image", "brief": "Serum on a vanity", "batch": "1"})
check(r.status_code == 402 and "Downloads only for now" in r.get_data(as_text=True), "generation is refused (402 page)")
r = a.post("/app/generate", data={"csrf": csrf(a), "tool": "ad_image", "brief": "Serum on a vanity", "batch": "1"},
           headers={"Accept": "application/json"})
check(r.status_code == 402 and r.get_json()["billing_hold"] is True
      and r.get_json()["error"] == billing_holds.HOLD_MESSAGE, "JSON callers get 402 billing_hold")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (A["id"],)).fetchone()[0] == 0, "no job was queued")
    conn.execute("UPDATE workspaces SET credits=5000 WHERE id=?", (A["id"],))
    conn.commit()
check(not spend(A["id"], 10), "no credit spend goes through while held, even with credits")
with db.connect() as conn:
    jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, credits, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (A["id"], 1, "ad_image", "pending", 22, db.now(), db.now())).lastrowid
    conn.execute("INSERT INTO ledger (workspace_id, delta, reason, job_id, created_at) VALUES (?,?,?,?,?)",
                 (A["id"], -22, "generation", jid, db.now()))
    conn.commit()
worker.tick()
with db.connect() as conn:
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
check(j["status"] == "failed" and j["error"].startswith("Paused: a payment on this workspace was reversed.")
      and ws(A["id"])["credits"] == 5022, "a job queued before the hold never starts and its credits go back")

print("== admin")
adm = signup("boss@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE email='boss@example.com'")
    conn.commit()
html = adm.get("/admin").get_data(as_text=True)
check("Billing holds" in html and f"#{A['id']} " in html and "evt_refund_a" not in html and "ch_pi_a" in html,
      "admin lists the hold and its reversal")
r = adm.post("/admin/credits", data={"csrf": csrf(adm), "workspace_id": A["id"], "amount": "-22", "reason": "fix"})
check(ws(A["id"])["credits"] == 5000, "an admin adjustment still works on a held workspace")
r = adm.post(f"/admin/workspaces/{A['id']}/lift-hold", data={"csrf": csrf(adm)})
w = ws(A["id"])
check(r.status_code == 302 and w["billing_hold"] == 0 and w["credits"] == 5000, "Lift hold: back to normal, credits as they were")
check("A payment was reversed" not in a.get("/app/library").get_data(as_text=True), "the banner is gone")
check(spend(A["id"], 10), "spending works again")

print("== partial refunds, in any order")
signup("bo@example.com")
B = ws_of("bo@example.com")
b0 = B["credits"]
buy_pack(B["id"], "pi_b")
r = deliver("charge.refunded", charge("pi_b", 1450))
check(r.get_json()["voided"] == 1450 and ws(B["id"])["credits"] == b0 + 1450 and ws(B["id"])["billing_hold"] == 1,
      "half refunded: half the credits voided, on hold")
deliver("charge.refunded", charge("pi_b", 2900))
check(ws(B["id"])["credits"] == b0, "then the rest: the rest voided")
r = deliver("charge.refunded", charge("pi_b", 1450))
check(r.get_json()["voided"] == 0 and ws(B["id"])["credits"] == b0, "an older event arriving late changes nothing")

print("== disputes")
signup("cy@example.com")
C = ws_of("cy@example.com")
c0 = C["credits"]
buy_pack(C["id"], "pi_c")
r = deliver("charge.dispute.created", dispute("pi_c", "dp_c"))
check(r.get_json()["voided"] == 2900 and ws(C["id"])["billing_hold"] == 1, "dispute opened: credits voided, on hold")
r = deliver("charge.dispute.closed", dispute("pi_c", "dp_c", status="won"))
w = ws(C["id"])
check(r.get_json()["restored"] == 2900 and w["credits"] == c0 + 2900 and w["billing_hold"] == 0,
      "dispute won: credits back, hold lifted")
r = deliver("charge.dispute.closed", dispute("pi_c", "dp_c", status="won"))
check(ws(C["id"])["credits"] == c0 + 2900, "a second won event restores nothing more")
signup("di@example.com")
D = ws_of("di@example.com")
buy_pack(D["id"], "pi_d")
deliver("charge.dispute.created", dispute("pi_d", "dp_d"))
r = deliver("charge.dispute.closed", dispute("pi_d", "dp_d", status="lost"))
with db.connect() as conn:
    st = conn.execute("SELECT status FROM credit_reversals WHERE stripe_id='dp_d'").fetchone()[0]
check(ws(D["id"])["billing_hold"] == 1 and st == "lost", "dispute lost: stays on hold")
# a refund and a won dispute on one workspace: the refund still holds it
signup("ed@example.com")
E = ws_of("ed@example.com")
buy_pack(E["id"], "pi_e1")
buy_pack(E["id"], "pi_e2")
deliver("charge.refunded", charge("pi_e1", 2900))
deliver("charge.dispute.created", dispute("pi_e2", "dp_e2"))
deliver("charge.dispute.closed", dispute("pi_e2", "dp_e2", status="won"))
check(ws(E["id"])["billing_hold"] == 1, "a won dispute doesn't lift a hold another refund put there")
# a refund then a dispute on the same payment never voids more than it bought
signup("fa@example.com")
F = ws_of("fa@example.com")
f0 = F["credits"]
buy_pack(F["id"], "pi_f")
deliver("charge.refunded", charge("pi_f", 1000))
deliver("charge.dispute.created", dispute("pi_f", "dp_f", amount=2900))
check(ws(F["id"])["credits"] == f0, "refund + dispute on one payment void at most its 2,900 credits")

print("== the refund that beats the checkout event")
signup("gi@example.com")
G = ws_of("gi@example.com")
g0 = G["credits"]
sess = buy_pack(G["id"], "pi_g", deliver_it=False)
r = deliver("charge.refunded", charge("pi_g", 2900))
check(r.get_json().get("ignored") and ws(G["id"])["billing_hold"] == 0, "refund first: parked, nothing to void yet")
r = deliver("checkout.session.completed", sess)
w = ws(G["id"])
check(r.status_code == 200 and w["credits"] == g0 and w["billing_hold"] == 1,
      "then the checkout: credited and voided at once, on hold")

print("== plan invoices")
signup("hu@example.com")
H = ws_of("hu@example.com")
p = plans.PLANS["starter"]


def invoice(inv_id, wid, sub, pi=None):
    inv = {"id": inv_id, "object": "invoice", "status": "paid", "currency": "usd", "customer": "cus_h",
           "billing_reason": "subscription_create", "amount_paid": 3900, "subscription": sub,
           "subscription_details": {"metadata": {"app": "campaignify", "kind": "plan", "workspace_id": str(wid)}},
           "lines": {"data": [{"amount": 3900, "price": {"id": p["price_id"]}, "period": {"end": 1893456000}}]}}
    if pi:
        inv["payment_intent"] = pi
    return inv


deliver("invoice.paid", invoice("in_h", H["id"], "sub_h", pi="pi_h"))
check(ws(H["id"])["plan_credits"] == 2000, "plan invoice grants its credits")
r = deliver("charge.refunded", charge("pi_h", 3900, amount=3900))
w = ws(H["id"])
check(r.get_json()["voided"] == 2000 and w["plan_credits"] == 0 and w["billing_hold"] == 1,
      "a refunded plan invoice voids its plan credits (older API: invoice.payment_intent)")
signup("ia@example.com")
I = ws_of("ia@example.com")
deliver("invoice.paid", invoice("in_i", I["id"], "sub_i"))
INVOICE_PAYMENTS["pi_i"] = "in_i"
r = deliver("charge.dispute.created", dispute("pi_i", "dp_i", amount=3900))
check(r.get_json()["voided"] == 2000 and ws(I["id"])["billing_hold"] == 1,
      "newer API: the invoice is found through Stripe's invoice payments (read only)")

print("== not ours, and the switch")
before = [ws(x["id"])["billing_hold"] for x in (A, C)]
r = deliver("charge.refunded", charge("pi_other_app", 5000, amount=5000))
check(r.get_json().get("ignored") and [ws(x["id"])["billing_hold"] for x in (A, C)] == before,
      "another app's refund on the shared account holds nobody")
os.environ["BILLING_HOLD"] = "off"
signup("jo@example.com")
J = ws_of("jo@example.com")
buy_pack(J["id"], "pi_j")
r = deliver("charge.refunded", charge("pi_j", 2900))
check(r.get_json().get("ignored") and ws(J["id"])["billing_hold"] == 0, "BILLING_HOLD=off: refunds are ignored as before")
os.environ.pop("BILLING_HOLD")

print("== event ids, every webhook")
sub = {"id": "sub_h", "object": "subscription", "status": "active", "customer": "cus_h", "cancel_at_period_end": False,
       "metadata": {"app": "campaignify", "workspace_id": str(H["id"])},
       "items": {"data": [{"price": {"id": p["price_id"]}, "current_period_end": 1893456000}]}}
r1 = deliver("customer.subscription.updated", sub, eid="evt_sub_same")
r2 = deliver("customer.subscription.updated", sub, eid="evt_sub_same")
check(r1.get_json().get("plan") == "starter" and r2.get_json().get("duplicate"), "subscription events are deduplicated too")
r = deliver("customer.subscription.updated", dict(sub, metadata={"app": "other"}, id="sub_zz"), eid="evt_ign")
with db.connect() as conn:
    check(conn.execute("SELECT 1 FROM stripe_events WHERE id='evt_ign'").fetchone() is None,
          "ignored events aren't recorded (a later delivery still runs)")

print(f"\nall {passed} checks passed ({TMP})")

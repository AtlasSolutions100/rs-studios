"""Discount codes from our email (Stripe promotion codes, e.g. TRIAL30) on a
throwaway database, Stripe faked at the HTTP layer.

    python tests/test_promo_codes.py      (tests/test_app.py runs it too)
"""
import os
import sys
import tempfile
from html import unescape
import time
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_promo_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")  # sign-ups here go straight in; test_login.py checks the emailed-link step
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0",
                  RS_EMAIL_VERIFY="0", STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")

import app as appmod  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import plans  # noqa: E402

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


NOW = int(time.time())
PROMOS = {   # what Stripe holds: ours, an expired one of ours, another app's code on the shared account
    "TRIAL30": {"id": "promo_trial30", "object": "promotion_code", "active": True, "code": "TRIAL30",
                "expires_at": NOW + 14 * 86400, "metadata": {"app": "campaignify", "purpose": "trial_30"},
                "promotion": {"type": "coupon", "coupon": "L0CEL7lJ"}},
    "OLD20": {"id": "promo_old20", "object": "promotion_code", "active": True, "code": "OLD20",
              "expires_at": NOW - 60, "metadata": {"app": "campaignify"},
              "promotion": {"type": "coupon", "coupon": "L0CEL7lJ"}},
    "MORNING10": {"id": "promo_m10", "object": "promotion_code", "active": True, "code": "MORNING10",
                  "expires_at": None, "metadata": {"brand": "The Morning Upgrade"},
                  "promotion": {"type": "coupon", "coupon": "I86A6dPg"}},
}
COUPONS = {"L0CEL7lJ": {"id": "L0CEL7lJ", "percent_off": 30, "duration": "once", "valid": True},
           "I86A6dPg": {"id": "I86A6dPg", "amount_off": 1000, "duration": "once", "valid": True}}
gets, posts = [], []
stripe_down = [False]


def fake_get(url, params=None, auth=None, timeout=0):
    gets.append((url, params))
    if stripe_down[0]:
        return Resp({"error": {"message": "down"}}, 500)
    path = url.split("/v1", 1)[1]
    if path == "/promotion_codes":
        q = dict(params)
        p = PROMOS.get(q.get("code"))
        return Resp({"object": "list", "data": [p] if p else []})
    if path.startswith("/coupons/"):
        return Resp(COUPONS[path.rsplit("/", 1)[1]])
    raise AssertionError(f"unexpected Stripe GET {path}")


def fake_post(url, data=None, auth=None, timeout=0, headers=None):
    posts.append((url, data))
    n = len(posts)
    return Resp({"id": f"cs_test_{n}", "url": f"https://checkout.stripe.test/{n}"})


billing.requests.get = fake_get
billing.requests.post = fake_post
appmod._maybe_reconcile = lambda ws_id, force=False: 0

print("== billing helpers")
check(billing.normalize_promo(" trial30 ") == "TRIAL30" and billing.normalize_promo("a b") == ""
      and billing.normalize_promo("x" * 41) == "", "codes are upper-cased; junk is ignored")
check(billing.discount_label({"percent_off": 30, "duration": "once"}) == "30% off your first paid month"
      and billing.discount_label({"percent_off": 50, "duration": "repeating", "duration_in_months": 2})
      == "50% off your first 2 paid months"
      and billing.discount_label({"amount_off": 1000, "duration": "once"}) == "$10 off your first paid month",
      "the label says exactly what the coupon gives")
p = billing.find_promotion_code("trial30")
check(p and p["id"] == "promo_trial30" and p["label"] == "30% off your first paid month",
      "our code is found, with the coupon read for its label")
check(billing.find_promotion_code("OLD20") is None, "an expired code is not used")
check(billing.find_promotion_code("MORNING10") is None, "another app's code on the shared account is never applied")
check(billing.find_promotion_code("NOPE99") is None, "an unknown code is None")


def form_of(i=-1):
    return dict(posts[i][1])


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    return c


def promo_in(c):
    with c.session_transaction() as s:
        return s.get("promo")


print("== no code: checkout keeps Stripe's own code field")
a = signup("ann@example.com")
a.post("/app/billing/subscribe", data={"csrf": csrf(a), "plan": "starter"})
f = form_of()
check(f.get("allow_promotion_codes") == "true" and "discounts[0][promotion_code]" not in f,
      "plain plan checkout: allow_promotion_codes, no discount")

print("== the email link")
r = a.get("/app/billing?code=trial30")
page = r.get_data(as_text=True)
check(r.status_code == 200 and "TRIAL30 saved: 30% off your first paid month" in page
      and "Code TRIAL30 saved" in page, "?code= saves the code and says what it gives")
check(promo_in(a)["id"] == "promo_trial30", "kept in the session for checkout")
r = a.post("/app/billing/subscribe", data={"csrf": csrf(a), "plan": "growth"})
f = form_of()
check(r.status_code == 303 and f.get("discounts[0][promotion_code]") == "promo_trial30"
      and "allow_promotion_codes" not in f and f["line_items[0][price]"] == plans.PLANS["growth"]["price_id"],
      "plan checkout applies the code (and drops allow_promotion_codes: Stripe takes one or the other)")
check(f["metadata[app]"] == "campaignify" and f["subscription_data[metadata][plan]"] == "growth",
      "our metadata is still on the session and the subscription")
a.get("/app/billing?plan=success&session_id=cs_test_x")
check(promo_in(a) is None, "a finished plan checkout forgets the code")

print("== codes that can't be used")
b = signup("bo@example.com")
page = unescape(b.get("/app/billing?code=OLD20").get_data(as_text=True))
check("OLD20 isn't valid or has expired" in page and promo_in(b) is None, "an expired code is refused, politely")
page = unescape(b.get("/app/billing?code=MORNING10").get_data(as_text=True))
check("isn't valid" in page and promo_in(b) is None, "another app's code is refused")
raw = b.get("/app/billing?code=%3Cscript%3E").get_data(as_text=True)
check("doesn't look right" in unescape(raw) and "&lt;script&gt;" not in raw and "<script>alert" not in raw, "junk in the link is refused and never echoed")

print("== typed into Have a code?")
r = b.post("/app/billing/code", data={"csrf": csrf(b), "code": " Trial30 "})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/billing#plans")
      and promo_in(b)["code"] == "TRIAL30", "a discount code typed on Billing is saved for checkout")
r = b.post("/app/billing/code", data={"csrf": csrf(b), "code": "ABCD-EFGH"})
check(r.status_code == 302 and "#code" in r.headers["Location"], "free-month codes still go to compcodes")

print("== the code expires between the link and checkout")
PROMOS["TRIAL30"]["expires_at"] = NOW - 1
r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "starter"})
f = form_of()
check(r.status_code == 303 and "discounts[0][promotion_code]" not in f and f.get("allow_promotion_codes") == "true"
      and promo_in(b) is None, "re-checked at checkout: an expired code is dropped, the Stripe code field stays")
PROMOS["TRIAL30"]["expires_at"] = NOW + 14 * 86400

print("== Stripe unreachable")
c = signup("cy@example.com")
stripe_down[0] = True
r = c.get("/app/billing?code=TRIAL30")
check(r.status_code == 200 and "enter it at checkout" in r.get_data(as_text=True), "the page still loads and says what to do")
stripe_down[0] = False

print("== logged out: the link survives the login")
lo = client()
r = lo.get("/app/billing?code=TRIAL30")
loc = r.headers["Location"]
check(r.status_code == 302 and parse_qs(urlparse(loc).query)["next"] == ["/app/billing?code=TRIAL30"],
      "login keeps the code in next")
r = lo.post(loc, data={"csrf": csrf(lo), "email": "cy@example.com", "password": "longpassword"})
check(r.status_code == 302 and r.headers["Location"] == "/app/billing?code=TRIAL30", "after login, back to the link")
check("TRIAL30 saved" in lo.get(r.headers["Location"]).get_data(as_text=True), "and the code is saved")
check(client().get("/app/billing").headers["Location"].endswith("next=/app/billing")
      or "next=%2Fapp%2Fbilling" in client().get("/app/billing").headers["Location"], "a plain path is unchanged")

print("== subscribed workspaces aren't offered a code")
with db.connect() as conn:
    wid = conn.execute("SELECT workspace_id FROM users WHERE email='ann@example.com'").fetchone()[0]
    conn.execute("UPDATE workspaces SET stripe_subscription_id='sub_1', stripe_customer_id='cus_1', plan='growth', "
                 "plan_status='active' WHERE id=?", (wid,))
    conn.commit()
page = a.get("/app/billing?code=TRIAL30").get_data(as_text=True)
check("TRIAL30 saved" not in page and promo_in(a) is None, "already paying: the link does nothing")

print(f"\nall {passed} checks passed ({TMP})")

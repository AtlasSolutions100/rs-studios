"""Campaignify's Stripe objects in whichever account the key belongs to (stripe_setup.py), against a pretend Stripe:

  * the account the configured ids live in: nothing is created, the ids stay
  * an empty account (Campaignify's own): three plan products and prices, three pack products and prices and the
    billing portal are made with the amounts the pricing page shows, and the app uses them (plans.PLANS,
    plans.BY_PRICE, catalog.PACKS, billing.PORTAL_CONFIG)
  * a second run, and a fresh process, reuse what was settled and create nothing
  * a price under our lookup key with another amount is left alone; a new one takes the key
  * Stripe refusing the portal configuration doesn't stop the plans and packs
  * ids from the old account: plan checkout, the card step, the portal and the plan sync set a customer or
    subscription Stripe doesn't know aside (kept in stripe_old_json) and carry on
  * off unless STRIPE_SETUP=1; Stripe's code and param ride on BillingError

    python tests/test_stripe_setup.py      (tests/test_app.py runs it too)
"""
import copy
import html as htmllib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_stripesetup_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "STRIPE_SECRET_KEY",
          "STRIPE_WEBHOOK_SECRET", "STRIPE_SETUP", "MCP_SERVER", "RS_LIVE_IDS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import liveids  # noqa: E402
import plans  # noqa: E402
import stripe_setup  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


ORIGINAL = {"plans": copy.deepcopy(plans.PLANS), "packs": copy.deepcopy(catalog.PACKS), "portal": billing.PORTAL_CONFIG}
OLD = dict(liveids.PRODUCTION)


def fresh_process():
    """What a newly started process holds: the configured ids, nothing applied."""
    for code, p in ORIGINAL["plans"].items():
        plans.PLANS[code].update(price_id=p["price_id"], product_id=p["product_id"])
    plans.BY_PRICE.clear()
    plans.BY_PRICE.update({p["price_id"]: p for p in plans.PAID})
    for p, o in zip(catalog.PACKS, ORIGINAL["packs"]):
        p["price_id"] = o["price_id"]
    billing.PORTAL_CONFIG = ORIGINAL["portal"]
    stripe_setup._state.update(applied=None, thread=None, error=None)


class Stripe:
    """Just enough of Stripe: one account's products, prices and portal configurations."""

    def __init__(self, acct, refuse_portal=False):
        self.acct, self.products, self.prices, self.portals = acct, {}, {}, {}
        self.posts, self.n, self.refuse_portal = [], 0, refuse_portal

    def _new(self, prefix):
        self.n += 1
        return f"{prefix}_{self.acct[-4:]}{self.n:03d}"

    def get(self, path, params=None):
        params = params or []
        if path == "/account":
            return {"id": self.acct}
        if path == "/products":
            return {"data": list(self.products.values())}
        if path == "/prices":
            keys = [v for k, v in params if k == "lookup_keys[]"]
            return {"data": [p for p in self.prices.values() if p.get("lookup_key") in keys and p["active"]][:1]}
        if path == "/billing_portal/configurations":
            return {"data": [c for c in self.portals.values() if c["active"]]}
        kind, _, oid = path.strip("/").rpartition("/")
        store = {"prices": self.prices, "billing_portal/configurations": self.portals}.get(kind)
        if store is not None and oid in store:
            return store[oid]
        raise billing.BillingError(f"Stripe 404: No such {kind}: '{oid}'", 404, "resource_missing", "id")

    def post(self, path, form, idempotency_key=None):
        f = {}
        for k, v in form:
            f.setdefault(k, []).append(v)
        one = lambda k: (f.get(k) or [None])[0]  # noqa: E731
        meta = {k[9:-1]: v[0] for k, v in f.items() if k.startswith("metadata[")}
        self.posts.append((path, f, idempotency_key))
        if path == "/products":
            p = {"id": self._new("prod"), "active": True, "name": one("name"), "description": one("description"),
                 "metadata": meta}
            self.products[p["id"]] = p
            return p
        if path == "/prices":
            key = one("lookup_key")
            for other in self.prices.values():
                if other.get("lookup_key") == key:
                    assert one("transfer_lookup_key") == "true", "a taken lookup key needs transfer_lookup_key"
                    other["lookup_key"] = None
            p = {"id": self._new("price"), "active": True, "product": one("product"), "currency": one("currency"),
                 "unit_amount": int(one("unit_amount")), "lookup_key": key, "metadata": meta,
                 "recurring": {"interval": one("recurring[interval]")} if one("recurring[interval]") else None}
            self.prices[p["id"]] = p
            return p
        if path == "/billing_portal/configurations":
            if self.refuse_portal:
                raise billing.BillingError("Stripe 400: Received unknown parameter", 400, "parameter_unknown", "features")
            rows = {}
            for k, v in f.items():
                if k.startswith("features[subscription_update][products]["):
                    i = k.split("[products][")[1].split("]")[0]
                    rows.setdefault(i, {"prices": []})
                    if k.endswith("[product]"):
                        rows[i]["product"] = v[0]
                    else:
                        rows[i]["prices"] += v
            c = {"id": self._new("bpc"), "active": True, "metadata": meta, "form": f,
                 "features": {"subscription_update": {"products": list(rows.values())}}}
            self.portals[c["id"]] = c
            return c
        raise AssertionError(f"unexpected POST {path}")


def use(stripe):
    billing._get, billing._post = stripe.get, stripe.post


os.environ["STRIPE_SECRET_KEY"] = "sk_live_pretend"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_pretend"

print("the account the configured ids live in")
home = Stripe("acct_HOME")
for name, oid in OLD.items():
    if name.startswith("STRIPE_PLAN_PRICE_") or name.startswith("STRIPE_PRICE_"):
        home.prices[oid] = {"id": oid, "active": True, "product": OLD.get(name.replace("PRICE", "PRODUCT"), "prod_pack"),
                            "unit_amount": 1, "currency": "usd", "lookup_key": None}
home.portals[OLD["STRIPE_PORTAL_CONFIG"]] = {"id": OLD["STRIPE_PORTAL_CONFIG"], "active": True, "metadata": {}}
use(home)
row = stripe_setup.ensure(wait_s=0)
check(row and row["account"] == "acct_HOME" and row["mode"] == "live", "the row names the account and mode it was settled for")
check(home.posts == [], "nothing is created where the configured ids already exist")
check(all(row["ids"][n] == OLD[n] for n in OLD if n.startswith("STRIPE_")), "every id stays the configured one")
check(plans.PLANS["growth"]["price_id"] == OLD["STRIPE_PLAN_PRICE_GROWTH"] and billing.PORTAL_CONFIG == OLD["STRIPE_PORTAL_CONFIG"],
      "the app keeps using them")

print("Campaignify's own account, empty")
fresh_process()
own = Stripe("acct_OWN1")
use(own)
row = stripe_setup.ensure(wait_s=0)
made = [p for p, _, _ in own.posts]
check(made.count("/products") == 6 and made.count("/prices") == 6 and made.count("/billing_portal/configurations") == 1,
      "six products, six prices and one portal configuration are made")
by_key = {p["lookup_key"]: p for p in own.prices.values()}
check([by_key[f"campaignify_plan_{c}_month"]["unit_amount"] for c in ("starter", "growth", "scale")] == [3900, 9900, 24900]
      and all(by_key[f"campaignify_plan_{c}_month"]["recurring"] == {"interval": "month"} for c in ("starter", "growth", "scale")),
      "plans are monthly at $39, $99 and $249, the pricing page's amounts")
check([by_key[f"campaignify_pack_{c}"]["unit_amount"] for c in ("starter", "pro", "studio")] == [2900, 7900, 19900]
      and all(by_key[f"campaignify_pack_{c}"]["recurring"] is None for c in ("starter", "pro", "studio")),
      "packs are one-time at $29, $79 and $199")
names = sorted(p["name"] for p in own.products.values())
check(names == ["Campaignify Growth", "Campaignify Pro pack", "Campaignify Scale", "Campaignify Starter",
                "Campaignify Starter pack", "Campaignify Studio pack"], "products carry the names customers saw before")
pro = next(p for p in own.products.values() if p["metadata"].get("pack") == "pro")
check("8,500 Campaignify credits (7,900 + 600 bonus)" in pro["description"], "a pack's description states its credits and bonus")
check(plans.PLANS["scale"]["price_id"] == by_key["campaignify_plan_scale_month"]["id"]
      and plans.PLANS["scale"]["product_id"] == by_key["campaignify_plan_scale_month"]["product"],
      "plans.PLANS points at the new price and product")
check(set(plans.BY_PRICE) == {by_key[f"campaignify_plan_{c}_month"]["id"] for c in ("starter", "growth", "scale")}
      and OLD["STRIPE_PLAN_PRICE_SCALE"] not in plans.BY_PRICE, "the webhook's price map holds the new prices only")
check(catalog.PACK_BY_CODE["studio"]["price_id"] == by_key["campaignify_pack_studio"]["id"], "catalog.PACKS points at the new pack prices")
portal = next(iter(own.portals.values()))
check(billing.PORTAL_CONFIG == portal["id"] and portal["metadata"].get("site") == "campaignify.ai", "the portal configuration is ours and in use")
pf = portal["form"]
check(pf["features[subscription_cancel][mode]"] == ["at_period_end"] and pf["features[subscription_update][proration_behavior]"] == ["always_invoice"]
      and pf["features[subscription_update][schedule_at_period_end][conditions][0][type]"] == ["decreasing_item_amount"],
      "portal: cancel at period end, upgrades invoiced right away, downgrades at period end")
check({pr for r in portal["features"]["subscription_update"]["products"] for pr in r["prices"]} == set(plans.BY_PRICE),
      "portal: our three plans, nothing else")
check(len({k for _, _, k in own.posts}) == len(own.posts) and all(k and "acct_OWN1-live" in k for _, _, k in own.posts),
      "every create carries its own idempotency key, tied to the account and mode")

print("again, and in a fresh process")
before = len(own.posts)
stripe_setup.ensure(wait_s=0)
check(len(own.posts) == before, "a second run creates nothing")
settled = dict(row["ids"])
fresh_process()
check(plans.PLANS["scale"]["price_id"] == OLD["STRIPE_PLAN_PRICE_SCALE"], "(a fresh process starts on the configured ids)")
stripe_setup.ensure(wait_s=0)
check(len(own.posts) == before and plans.PLANS["scale"]["price_id"] == settled["STRIPE_PLAN_PRICE_SCALE"]
      and billing.PORTAL_CONFIG == settled["STRIPE_PORTAL_CONFIG"], "a fresh process takes the settled ids and creates nothing")
check(stripe_setup.status() == "off", "status is off while STRIPE_SETUP isn't on")
os.environ["STRIPE_SETUP"] = "1"
check(stripe_setup.status() == "ok", "status is ok once the ids are settled")
os.environ.pop("STRIPE_SETUP")

print("another process is doing it")
fresh_process()
other = Stripe("acct_OTHR")
use(other)
with db.connect() as conn:
    conn.execute("DELETE FROM worker_leases WHERE name=?", (stripe_setup.META,))
    conn.commit()
    assert db.lease(conn, stripe_setup.META, "someone-else", ttl_s=60)
check(stripe_setup.ensure(wait_s=0) is None and other.posts == [], "while another process holds the work, nothing is created here")
with db.connect() as conn:
    conn.execute("DELETE FROM worker_leases WHERE name=?", (stripe_setup.META,))
    conn.commit()

print("a price under our key with another amount")
fresh_process()
odd = Stripe("acct_ODD1")
odd.products["prod_x"] = {"id": "prod_x", "active": True, "metadata": {"brand": "Campaignify", "plan": "starter"}}
odd.prices["price_was"] = {"id": "price_was", "active": True, "product": "prod_x", "currency": "usd", "unit_amount": 2900,
                           "lookup_key": "campaignify_plan_starter_month", "recurring": {"interval": "month"}}
use(odd)
row = stripe_setup.ensure(wait_s=0)
new = row["ids"]["STRIPE_PLAN_PRICE_STARTER"]
check(new != "price_was" and odd.prices[new]["unit_amount"] == 3900 and odd.prices[new]["lookup_key"] == "campaignify_plan_starter_month",
      "a new $39 price takes the lookup key")
check(odd.prices["price_was"]["active"] and odd.prices["price_was"]["unit_amount"] == 2900, "the old price is not edited or archived")
check(odd.prices[new]["product"] == "prod_x" and sum(1 for p, _, _ in odd.posts if p == "/products") == 5,
      "the product already tagged as ours is reused")

print("Stripe refuses the portal configuration")
fresh_process()
no_portal = Stripe("acct_NOPT", refuse_portal=True)
use(no_portal)
row = stripe_setup.ensure(wait_s=0)
check(row and row["ids"]["STRIPE_PORTAL_CONFIG"] is None and billing.PORTAL_CONFIG is None,
      "the portal falls back to the account's own settings")
check(plans.PLANS["growth"]["price_id"] in no_portal.prices, "plans and packs are still settled")

print("off unless switched on")
fresh_process()
stripe_setup.start()
check(stripe_setup._state["thread"] is None, "start() does nothing without STRIPE_SETUP=1")
stripe_setup.ready()
check(stripe_setup._state["thread"] is None, "ready() does nothing either")
e = billing.BillingError("Stripe 400: No such customer: 'cus_old'", 400, "resource_missing", "customer")
check(billing.missing(e, "customer") and billing.missing(e) and not billing.missing(e, "price")
      and not billing.missing(billing.BillingError("Stripe unreachable")), "BillingError carries Stripe's code and param")

print("ids from the old account")
fresh_process()
use(own)
stripe_setup.ensure(wait_s=0)
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


def signup(email, **cols):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        for k, v in cols.items():
            conn.execute(f"UPDATE workspaces SET {k}=? WHERE id=?", (v, u["workspace_id"]))
        conn.commit()
    return c, u["workspace_id"]


def ws_row(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


calls = []


def fake_sub_session(**kw):
    calls.append(kw)
    if kw.get("customer"):
        raise billing.BillingError("Stripe 400: No such customer: 'cus_old'", 400, "resource_missing", "customer")
    return {"id": f"cs_{len(calls)}", "url": "https://checkout.stripe.test/c/pay"}


billing.create_subscription_session = fake_sub_session
billing.find_promotion_code = lambda code: None
a, wa = signup("a@example.com", card_customer_id="cus_old")
r = a.post("/app/billing/subscribe", data={"csrf": csrf(a), "plan": "growth"})
check(r.status_code == 303 and r.headers["Location"].startswith("https://checkout.stripe.test/"), "plan checkout still opens")
check([c.get("customer") for c in calls] == ["cus_old", None] and calls[-1]["price_id"] == settled["STRIPE_PLAN_PRICE_GROWTH"],
      "it tried the old customer once, then started fresh on the new account's price")
row = ws_row(wa)
check(row["card_customer_id"] is None and "cus_old" in (row["stripe_old_json"] or ""), "the old customer id is set aside, not deleted")

calls.clear()
billing.create_subscription_session = lambda **kw: (_ for _ in ()).throw(billing.BillingError("Stripe 402: card declined", 402, "card_declined"))
b, wb = signup("b@example.com", card_customer_id="cus_keep")
r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "growth"})
check(r.status_code == 302 and ws_row(wb)["card_customer_id"] == "cus_keep", "any other Stripe error leaves the customer id alone")

# Stripe's own reason is added to the message for a site admin, and only for a site admin
r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "growth"}, follow_redirects=True)
check("Checkout couldn't start. Try again in a minute." in htmllib.unescape(r.get_data(as_text=True))
      and "Stripe said" not in r.get_data(as_text=True), "a member sees that checkout couldn't start, without Stripe's words")
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE workspace_id=?", (wb,))
    conn.commit()
r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "growth"}, follow_redirects=True)
check("Stripe said: Stripe 402: card declined" in htmllib.unescape(r.get_data(as_text=True)), "a site admin also sees what Stripe said")

portal_calls = []


def fake_portal(**kw):
    portal_calls.append(kw)
    raise billing.BillingError("Stripe 400: No such customer: 'cus_gone'", 400, "resource_missing", "customer")


billing.create_portal_session = fake_portal
c, wc = signup("c@example.com", stripe_customer_id="cus_gone", stripe_subscription_id="sub_gone")
r = c.post("/app/billing/portal", data={"csrf": csrf(c)}, follow_redirects=True)
row = ws_row(wc)
check("no subscription to manage here" in r.get_data(as_text=True) and row["stripe_customer_id"] is None
      and row["stripe_subscription_id"] is None, "the portal says there is nothing to manage here and clears the old ids")
old = json.loads(row["stripe_old_json"])
check({o["id"] for o in old} == {"cus_gone", "sub_gone"} and all(o["at"] for o in old), "both are kept with the date")

d, wd = signup("d@example.com", stripe_subscription_id="sub_old")


def sync_get(path, params=None):
    if path.startswith("/subscriptions/"):
        raise billing.BillingError("Stripe 404: No such subscription: 'sub_old'", 404, "resource_missing", "id")
    return own.get(path, params)


billing._get = sync_get
with appmod.app.app_context():
    appmod._reconcile_plan(wd)
check(ws_row(wd)["stripe_subscription_id"] is None and "sub_old" in ws_row(wd)["stripe_old_json"],
      "the plan sync sets a subscription from another account aside instead of asking for it every hour")

os.environ["RS_CARD_REQUIRED"] = "1"
made_customers, setups = [], []


def fake_customer(**kw):
    made_customers.append(kw)
    return "cus_new"


def fake_setup(**kw):
    setups.append(kw["customer"])
    if kw["customer"] == "cus_old":
        raise billing.BillingError("Stripe 400: No such customer: 'cus_old'", 400, "resource_missing", "customer")
    return {"id": "cs_setup", "url": "https://checkout.stripe.test/c/setup"}


billing.create_customer, billing.create_setup_session = fake_customer, fake_setup
f, wf = signup("f@example.com", card_customer_id="cus_old")
import card_routes  # noqa: E402
start_url = next(str(r) for r in appmod.app.url_map.iter_rules() if r.endpoint == "card.start")
r = f.post(start_url, data={"csrf": csrf(f)})
check(r.status_code == 303 and setups == ["cus_old", "cus_new"] and ws_row(wf)["card_customer_id"] == "cus_new",
      "the card step makes a new customer in this account and opens")
check("cus_old" in (ws_row(wf)["stripe_old_json"] or "") and made_customers[0]["idempotency_key"] != f"campaignify-card-customer-{wf}",
      "the old card customer is set aside and the new one isn't a replay of the old request")
os.environ.pop("RS_CARD_REQUIRED")

r = appmod.app.test_client().get("/health")
check(r.get_json().get("billing_setup") == "off", "/health reports the setup state (off here)")
dy = open(os.path.join(ROOT, ".github", "workflows", "deploy.yml")).read()
check("STRIPE_SETUP: ${{ vars.STRIPE_SETUP || '1' }}" in dy and '"STRIPE_SETUP"' in dy, "deploy.yml switches it on in production")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

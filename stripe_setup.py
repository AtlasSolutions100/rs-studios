"""Campaignify's own Stripe objects, in whichever Stripe account the secret key belongs to (Nov 11 2026).

The three plan prices and products, the three credit-pack prices and the Billing Portal configuration are ids inside
ONE Stripe account (liveids.py). None of them exists in any other account, or in test mode: point the app at
Campaignify's own Stripe account and checkout would ask for prices that aren't there. So at start-up each process asks
Stripe which account and mode its key is for, and settles every id for that account:

  1. the configured id (liveids.py) when that object is in this account: nothing changes and nothing is created;
  2. else the object that carries our lookup key (prices) or our metadata (products, the portal configuration);
  3. else it is created with the same names, amounts and settings as the originals.

The ids it settles on are stored in meta["stripe_setup"], with the account, mode and amounts they were settled for,
and put where the app reads them: plans.PLANS and plans.BY_PRICE, catalog.PACKS, billing.PORTAL_CONFIG. One process
does the work (the "stripe_setup" lease); the others wait for its row. Amounts come from plans.PLANS and catalog.PACKS,
the numbers the pricing page shows. An existing price is never edited or archived: a changed amount gets a new price,
and the lookup key moves to it.

Ids kept from the old account (a workspace's Stripe customer, the customer its card was saved on, its subscription)
mean nothing in the new one. forget() is called where Stripe says "no such customer / subscription": the id is moved
to workspaces.stripe_old_json (kept, not deleted) and the column is cleared, so the member can check out again.

STRIPE_SETUP=1 turns the start-up step on. deploy.yml sets it; it is off in code, so tests and local runs never call
Stripe at import.
"""
import json
import logging
import os
import threading
import time

import billing
import catalog
import db
import liveids
import plans

log = logging.getLogger("campaignify.stripe_setup")

META = "stripe_setup"
BRAND = "Campaignify"
SITE = "campaignify.ai"
PLAN_ABOUT = {
    "starter": "ad studio, copy, AI campaign planner.",
    "growth": "everything in Starter plus SEO/AEO/GEO, pages, smart links, forms and contacts.",
    "scale": "everything in Growth plus community and courses, unlimited brands and more seats.",
}
CANCEL_REASONS = ("too_expensive", "missing_features", "unused", "low_quality", "switched_service", "too_complex",
                  "customer_service", "other")
_state = {"applied": None, "thread": None, "error": None}


def on() -> bool:
    return (os.environ.get("STRIPE_SETUP") or "").strip().lower() in ("1", "true", "yes", "on")


def mode() -> str:
    """"test" for a test-mode key, else "live". Test and live share one account id but not one object."""
    return "test" if "_test_" in billing.secret_key()[:16] else "live"


def plan_key(code: str) -> str:
    return f"campaignify_plan_{code}_month"


def pack_key(code: str) -> str:
    return f"campaignify_pack_{code}"


def wanted() -> dict:
    """What the ids are settled for: each amount in cents and each configured id. A change in either settles again."""
    out = {f"plan:{p['code']}": p["usd"] * 100 for p in plans.PAID}
    out.update({f"pack:{p['code']}": p["usd"] * 100 for p in catalog.PACKS})
    # what a product's description says: a pack that is re-cut, or a plan given other credits, settles again
    out.update({f"about:plan:{p['code']}": plan_about(p) for p in plans.PAID})
    out.update({f"about:pack:{p['code']}": pack_about(p) for p in catalog.PACKS})
    for name in sorted(liveids.PRODUCTION):
        if name.startswith("STRIPE_"):
            out[name] = liveids.get(name) or ""
    return out


# ---------------------------------------------------------------- reading Stripe
def _id(x):
    return x.get("id") if isinstance(x, dict) else x


def plan_about(p: dict) -> str:
    return f"Monthly plan: {p['credits']:,} credits each month, {PLAN_ABOUT.get(p['code'], p.get('line', ''))}"


def pack_about(p: dict) -> str:
    paid = p["credits"] - (p.get("bonus") or 0)
    split = f" ({paid:,} + {p['bonus']:,} bonus)" if p.get("bonus") else ""
    return f"{p['credits']:,} Campaignify credits{split} for making ads at {SITE}. One-time purchase."


def _describe(prod_id: str, about: str) -> None:
    """Checkout shows the product's description, so it has to say what the member gets today. Our own product
    (tagged brand Campaignify) whose description says something else is corrected; anything else is left alone."""
    prod = _fetch(f"/products/{prod_id}") if prod_id else None
    if not prod or (prod.get("metadata") or {}).get("brand") != BRAND or (prod.get("description") or "") == about:
        return
    billing._post(f"/products/{prod['id']}", [("description", about)])
    log.info("stripe setup: description of %s brought up to date", prod["id"])


def _fetch(path: str) -> dict | None:
    """The object at `path`, or None when it isn't in this account and mode."""
    try:
        return billing._get(path)
    except billing.BillingError as e:
        if billing.missing(e) or getattr(e, "status", None) == 404:
            return None
        raise


def _by_lookup(key: str) -> dict | None:
    data = billing._get("/prices", [("lookup_keys[]", key), ("active", "true"), ("limit", "1")]).get("data") or []
    return data[0] if data else None


def _product(have: dict, kind: str, code: str, name: str, about: str, extra: dict, tag: str) -> str:
    """Our product for a plan or a pack: the one tagged brand + plan/pack, else a new one. `have` caches the
    account's products, read once and only when a product is needed."""
    if have.get("rows") is None:
        have["rows"] = billing._get("/products", [("active", "true"), ("limit", "100")]).get("data") or []
    for p in have["rows"]:
        m = p.get("metadata") or {}
        if p.get("active") and m.get("brand") == BRAND and m.get(kind) == code:
            return p["id"]
    form = [("name", name), ("description", about), ("metadata[brand]", BRAND), (f"metadata[{kind}]", code)]
    form += [(f"metadata[{k}]", str(v)) for k, v in extra.items()]
    if kind == "pack":
        form.append(("url", f"https://{SITE}"))
    made = billing._post("/products", form, f"cfy-setup-{tag}-{kind}-{code}-product")
    have["rows"].append(made)
    return made["id"]


def _price(have: dict, kind: str, p: dict, configured: str | None, tag: str) -> tuple[str, str]:
    """(price id, product id) for one plan or pack in this account."""
    code, cents = p["code"], p["usd"] * 100
    about = plan_about(p) if kind == "plan" else pack_about(p)
    hit = _fetch(f"/prices/{configured}") if configured else None
    if hit and hit.get("active"):
        _describe(_id(hit.get("product")), about)
        return hit["id"], _id(hit.get("product"))
    key = plan_key(code) if kind == "plan" else pack_key(code)
    hit = _by_lookup(key)
    monthly = ((hit or {}).get("recurring") or {}).get("interval") == "month"
    if hit and hit.get("unit_amount") == cents and hit.get("currency") == "usd" and monthly == (kind == "plan"):
        _describe(_id(hit.get("product")), about)
        return hit["id"], _id(hit.get("product"))
    if kind == "plan":
        prod = _product(have, "plan", code, f"{BRAND} {p['name']}", about, {"monthly_credits": p["credits"]}, tag)
    else:
        prod = _product(have, "pack", code, f"{BRAND} {p['name']} pack", about, {"credits": p["credits"]}, tag)
    _describe(prod, about)          # a product found by its tag may still carry an older description
    form = [("product", prod), ("currency", "usd"), ("unit_amount", str(cents)), ("lookup_key", key),
            ("transfer_lookup_key", "true"), ("metadata[brand]", BRAND), (f"metadata[{kind}]", code),
            ("metadata[site]", SITE)]
    if kind == "plan":
        form += [("recurring[interval]", "month"), ("nickname", f"{p['name']} monthly")]
    else:
        form.append(("nickname", f"{p['name']} pack"))
    made = billing._post("/prices", form, f"cfy-setup-{tag}-{kind}-{code}-{cents}")
    return made["id"], prod


def _portal(ids: dict, tag: str) -> str:
    """The Billing Portal configuration for Campaignify: our three plans only, upgrades invoiced right away,
    downgrades and cancellations at period end."""
    configured = liveids.get("STRIPE_PORTAL_CONFIG")
    hit = _fetch(f"/billing_portal/configurations/{configured}") if configured else None
    if hit and hit.get("active"):
        return hit["id"]
    prices = [ids[f"STRIPE_PLAN_PRICE_{p['code'].upper()}"] for p in plans.PAID]
    for c in billing._get("/billing_portal/configurations", [("active", "true"), ("limit", "100")]).get("data") or []:
        if (c.get("metadata") or {}).get("site") != SITE:
            continue
        there = {pr for row in ((c.get("features") or {}).get("subscription_update") or {}).get("products") or []
                 for pr in row.get("prices") or []}
        if set(prices) <= there:
            return c["id"]
    form = [("name", BRAND), ("business_profile[headline]", "Manage your Campaignify plan"),
            ("business_profile[privacy_policy_url]", f"https://{SITE}/privacy"),
            ("business_profile[terms_of_service_url]", f"https://{SITE}/terms"),
            ("default_return_url", f"https://{SITE}/app/billing"),
            ("features[customer_update][enabled]", "true"),
            ("features[invoice_history][enabled]", "true"),
            ("features[payment_method_update][enabled]", "true"),
            ("features[subscription_cancel][enabled]", "true"),
            ("features[subscription_cancel][mode]", "at_period_end"),
            ("features[subscription_cancel][proration_behavior]", "none"),
            ("features[subscription_cancel][cancellation_reason][enabled]", "true"),
            ("features[subscription_update][enabled]", "true"),
            ("features[subscription_update][default_allowed_updates][]", "price"),
            ("features[subscription_update][proration_behavior]", "always_invoice"),
            ("features[subscription_update][schedule_at_period_end][conditions][0][type]", "decreasing_item_amount"),
            ("metadata[brand]", BRAND), ("metadata[site]", SITE)]
    form += [("features[customer_update][allowed_updates][]", x) for x in ("email", "name", "address", "tax_id")]
    form += [("features[subscription_cancel][cancellation_reason][options][]", x) for x in CANCEL_REASONS]
    for i, p in enumerate(plans.PAID):
        form.append((f"features[subscription_update][products][{i}][product]", ids[f"STRIPE_PLAN_PRODUCT_{p['code'].upper()}"]))
        form.append((f"features[subscription_update][products][{i}][prices][]", ids[f"STRIPE_PLAN_PRICE_{p['code'].upper()}"]))
    return billing._post("/billing_portal/configurations", form, f"cfy-setup-{tag}-portal-{'-'.join(prices)[-60:]}")["id"]


def resolve(acct: str, md: str) -> dict:
    """Every id for this account and mode: found, or made. {setting name: id}; the portal is None if Stripe refused it."""
    tag = f"{acct}-{md}"
    have: dict = {"rows": None}
    ids: dict = {}
    for kind, rows in (("plan", plans.PAID), ("pack", catalog.PACKS)):
        for p in rows:
            name = (f"STRIPE_PLAN_PRICE_{p['code'].upper()}" if kind == "plan" else f"STRIPE_PRICE_{p['code'].upper()}")
            price, prod = _price(have, kind, p, liveids.get(name), tag)
            ids[name] = price
            if kind == "plan":
                ids[f"STRIPE_PLAN_PRODUCT_{p['code'].upper()}"] = prod
    try:
        ids["STRIPE_PORTAL_CONFIG"] = _portal(ids, tag)
    except billing.BillingError as e:
        # the plans and packs still sell; "Manage plan" falls back to the account's own portal settings
        log.warning("stripe setup: the billing portal configuration could not be made: %s", e)
        ids["STRIPE_PORTAL_CONFIG"] = None
    return ids


# ---------------------------------------------------------------- using the ids
def apply(ids: dict) -> None:
    """Put settled ids where the app reads them (this process only)."""
    for p in plans.PAID:
        c = p["code"].upper()
        p["price_id"] = ids.get(f"STRIPE_PLAN_PRICE_{c}") or p["price_id"]
        p["product_id"] = ids.get(f"STRIPE_PLAN_PRODUCT_{c}") or p["product_id"]
    plans.BY_PRICE.clear()
    plans.BY_PRICE.update({p["price_id"]: p for p in plans.PAID})
    for p in catalog.PACKS:
        p["price_id"] = ids.get(f"STRIPE_PRICE_{p['code'].upper()}") or p["price_id"]
    if "STRIPE_PORTAL_CONFIG" in ids:
        billing.PORTAL_CONFIG = ids["STRIPE_PORTAL_CONFIG"]


def saved() -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (META,)).fetchone()
    try:
        return json.loads(row["value"]) if row and row["value"] else None
    except ValueError:
        return None


def _save(row: dict) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (META, json.dumps(row)))
        conn.commit()


def ensure(wait_s: float = 90.0, pause_s: float = 2.0) -> dict | None:
    """Make this process use the ids of the account its key belongs to. Returns the stored row, or None when another
    process holds the work and its row didn't arrive in time."""
    if not billing.secret_key():
        return None
    acct, md, want = billing._get("/account")["id"], mode(), wanted()
    holder = f"stripe-setup-{os.getpid()}-{threading.get_ident()}"
    deadline = time.time() + wait_s
    while True:
        row = saved()
        if row and row.get("account") == acct and row.get("mode") == md and row.get("want") == want and row.get("ids"):
            apply(row["ids"])
            _state.update(applied=(acct, md), error=None)
            return row
        with db.connect() as conn:
            mine = db.lease(conn, META, holder, ttl_s=180)
        if mine:
            ids = resolve(acct, md)
            row = {"account": acct, "mode": md, "want": want, "ids": ids, "at": db.now()}
            _save(row)
            apply(ids)
            _state.update(applied=(acct, md), error=None)
            log.info("stripe setup: ids settled for %s (%s mode)", acct, md)
            return row
        if time.time() >= deadline:
            return None
        time.sleep(pause_s)


def start() -> None:
    """Settle the ids on a background thread at start-up (STRIPE_SETUP=1 and a Stripe key). A failed try is retried
    a few times; until it works the app keeps the configured ids, as before."""
    t = _state["thread"]
    if not on() or not billing.secret_key() or _state["applied"] or (t is not None and t.is_alive()):
        return

    def run():
        for attempt in range(6):
            try:
                if ensure():
                    return
            except Exception as e:  # noqa: BLE001 - never take the process down
                _state["error"] = str(e)[:200]
                log.warning("stripe setup: try %s failed: %s", attempt + 1, e)
            time.sleep(20 * (attempt + 1))

    t = threading.Thread(target=run, name="rs-stripe-setup", daemon=True)
    _state["thread"] = t
    t.start()


def ready(timeout: float = 8.0) -> None:
    """Call before a price id is used (checkout, the portal, the webhook): right after a restart this waits for the
    start-up step. Afterwards it does nothing."""
    t = _state["thread"]
    if t is None or not t.is_alive():
        start()             # a process that never ran it, or whose tries ran out: once more, in the background
        t = _state["thread"]
    if t is not None and t.is_alive() and not _state["error"]:      # a failing Stripe never holds a request up
        t.join(timeout)


def status() -> str:
    """For /health: "off" (not switched on, or no key), "ok" (ids settled for this key; "ok, portal not set" when
    Stripe refused the portal configuration), "pending" or "error". No ids and no account are given out."""
    if not on() or not billing.secret_key():
        return "off"
    if _state["applied"]:
        return "ok" if billing.PORTAL_CONFIG else "ok, portal not set"
    return "error" if _state["error"] else "pending"


# ---------------------------------------------------------------- ids from another account
def forget(ws_id: int, *columns: str) -> list[str]:
    """Stripe said these ids don't exist in this account: keep them in workspaces.stripe_old_json and clear the
    columns, so the workspace can start again here. Returns the ids moved."""
    allowed = [c for c in columns if c in ("stripe_customer_id", "card_customer_id", "stripe_subscription_id")]
    if not allowed:
        return []
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        if not row:
            return []
        try:
            old = json.loads(row["stripe_old_json"] or "[]")
        except ValueError:
            old = []
        moved = []
        for c in allowed:
            if row[c]:
                old.append({"was": c, "id": row[c], "at": db.now()})
                moved.append(row[c])
        if moved:
            sets = ", ".join(f"{c}=NULL" for c in allowed)
            conn.execute(f"UPDATE workspaces SET {sets}, stripe_old_json=? WHERE id=?", (json.dumps(old[-40:]), ws_id))
            conn.commit()
            log.warning("workspace %s: Stripe ids not in this account were set aside (%s)", ws_id, ", ".join(allowed))
    return moved

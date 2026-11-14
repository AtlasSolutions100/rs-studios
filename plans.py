"""Plans, what each one includes, and the one place that decides access.

Three monthly plans on Stripe (recurring Prices on the shared account), plus
the free tier everyone starts on. Every plan includes everything in the plans
below it. Credit packs (catalog.PACKS) stay on sale as top-ups for anyone.
With PLANS_ONLY on (plan_gate.py) there is no free tier for new accounts: a
sign-up picks a plan, Starter and Growth start with a 3-day trial, and packs
top up a plan.

Credits come in two buckets on a workspace:
    plan_credits  granted by each paid plan invoice; they expire at the next
                  renewal (the new month's grant replaces whatever is left)
    credits       starter credits, purchased packs and admin top-ups; these
                  never expire
Spending always takes plan credits first (db.adjust_credits).

Use `has(ws, feature)` / `limit(ws, key)` everywhere; never compare plan names
by hand. A workspace's plan is its active Stripe subscription, an admin
"comp" plan (plan_override) when one is set, or a free-month code's plan
(comp_plan, until comp_until; compcodes.py), whichever is highest.
"""
import os
from datetime import datetime, timezone

import liveids

# Order matters: each plan includes everything in the ones before it.
ORDER = ["free", "starter", "growth", "scale"]

PLANS = {
    "free": {"code": "free", "name": "Free", "usd": 0, "credits": 0, "price_id": None, "product_id": None,
             "brands": 1, "seats": 1, "pages": 0,
             "line": "Your starter credits and the ad studio. Top up with credit packs."},
    "starter": {"code": "starter", "name": "Starter", "usd": 39, "next_usd": 79, "credits": 2000,
                "price_id": None, "product_id": None,
                "brands": 1, "seats": 1, "pages": 1,
                "line": "For one seller running their own campaigns."},
    "growth": {"code": "growth", "name": "Growth", "usd": 99, "next_usd": 149, "credits": 6000,
               "price_id": None, "product_id": None,
               "brands": 3, "seats": 3, "pages": None,
               "line": "For small teams and a few brands."},
    "scale": {"code": "scale", "name": "Scale", "usd": 249, "next_usd": 349, "credits": 18000,
              "price_id": None, "product_id": None,
              "brands": None, "seats": 10, "pages": None,
              "line": "For agencies and teams running many brands."},
}
# The Stripe ids are settings (liveids.py, M7.4): STRIPE_PLAN_PRICE_<CODE> and STRIPE_PLAN_PRODUCT_<CODE>.
for _code, _p in PLANS.items():
    if _code != "free":
        _p["price_id"] = liveids.get(f"STRIPE_PLAN_PRICE_{_code.upper()}")
        _p["product_id"] = liveids.get(f"STRIPE_PLAN_PRODUCT_{_code.upper()}")
PAID = [PLANS[c] for c in ORDER if c != "free"]
BY_PRICE = {p["price_id"]: p for p in PAID}


def sale_on() -> bool:
    """PLAN_SALE (default off): show each plan's price next to the price it is going to (`next_usd`, Koby,
    Nov 11 2026), struck through, with the difference as a percent off. Display only: what is charged is `usd`,
    the Stripe Price. Switch it off (or raise `usd`) when the prices go up."""
    return (os.environ.get("PLAN_SALE") or "").strip().lower() in ("1", "on", "true", "yes")


SALE_BY_DEFAULT = "2026-12-31"      # Koby, Nov 14 2026: the increase is real, by the end of the year at the latest


def sale_by() -> datetime | None:
    """PLAN_SALE_BY (an ISO date, default SALE_BY_DEFAULT): the date the prices go up by. The price line names it
    ("Goes to $79 by Dec 31"), so the urgency is a real, dated one rather than an undated strike-through. Past
    that date the line disappears by itself (sale() is None), so it can never go stale on the page."""
    raw = (os.environ.get("PLAN_SALE_BY") or "").strip() or SALE_BY_DEFAULT
    try:
        d = datetime.fromisoformat(raw[:10])
    except ValueError:
        d = datetime.fromisoformat(SALE_BY_DEFAULT)
    return d.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)


def sale(p, now: datetime | None = None) -> dict | None:
    """{"next": 79, "off": 50, "by": "Dec 31"} for a plan whose price is going up, while PLAN_SALE is on and the
    date has not passed; else None. The percent is rounded down, so it never says more than the difference."""
    nxt = (p or {}).get("next_usd")
    if not sale_on() or not nxt or nxt <= p["usd"]:
        return None
    by = sale_by()
    if (now or datetime.now(timezone.utc)) > by:
        return None
    return {"next": nxt, "off": (nxt - p["usd"]) * 100 // nxt, "by": f"{by:%b} {by.day}"}

# Subscription states Stripe still bills. They keep the plan on, except that
# past_due (the renewal payment failed, Stripe is retrying) pauses the
# workspace once the period paid for is over (renewal_paused).
LIVE_STATUSES = {"active", "trialing", "past_due"}
# The renewal failed and Stripe keeps the subscription: plan credits are kept
# but nothing can be spent until it's paid (RENEWAL_PAUSE, core.hold_check).
RENEWAL_FAILED = {"past_due", "unpaid", "paused"}
# Not billed (any more, or yet): the plan stays on only while a period that was
# paid for runs (plan_paid_through, set by each paid invoice). A paid period is
# paid usage; nothing is taken back early.
GRACE_STATUSES = {"canceled", "unpaid", "paused", "incomplete", "incomplete_expired", "pending"}


def _on(name: str) -> bool:
    return (os.environ.get(name) or "on").strip().lower() not in ("0", "off", "false", "no")


def pending_on() -> bool:
    """PLAN_PENDING (default on): a plan checkout waits for its invoice to be paid
    (plan_status='pending', "Confirming payment") before the plan switches on."""
    return _on("PLAN_PENDING")


def renewal_pause_on() -> bool:
    """RENEWAL_PAUSE (default on): a failed renewal stops generation at once."""
    return _on("RENEWAL_PAUSE")


def _key(ws, k):
    return ws[k] if k in ws.keys() else None


def paid_period_left(ws) -> bool:
    """True while the last period a paid invoice covered hasn't ended."""
    t = _key(ws, "plan_paid_through")
    return bool(t) and str(t) > _now_iso()


def renewal_paused(ws) -> bool:
    """The subscription's renewal payment failed and the paid period is over:
    downloads only until it's paid (plan credits are kept, not spent)."""
    return (renewal_pause_on() and ws is not None and bool(_key(ws, "stripe_subscription_id"))
            and _key(ws, "plan_status") in RENEWAL_FAILED and not paid_period_left(ws))


def lapse_due(ws) -> bool:
    """The subscription ended and the period it paid for is over, but the
    workspace still carries the plan: time to drop it (core.settle_plan_lapse)."""
    return (ws is not None and _key(ws, "plan_status") in ("canceled", "incomplete_expired")
            and _key(ws, "plan") not in (None, "free") and not paid_period_left(ws))

# feature -> {min plan, name, blurb, shipped}. `shipped` features are the only
# ones listed on pricing and features pages; flip it when a module lands.
FEATURES = {
    "studio": {"plan": "free", "name": "UGC ad studio", "shipped": True,
               "blurb": "AI presenter (UGC) videos, product videos, image ads and motion copies from your product photos."},
    "brand_kit": {"plan": "free", "name": "Brands & products", "shipped": True,
                  "blurb": "Every brand and product with its photos, offer, approved claims and audience; switch in one click and every brief starts from it."},
    "compliance": {"plan": "free", "name": "Compliance checks", "shipped": True,
                   "blurb": "Every brief is checked for income, health and recruiting claims and invented discounts."},
    "copy": {"plan": "starter", "name": "Copy studio", "shipped": True,
             "blurb": "Ad copy, hooks, captions, headlines, emails and landing copy for each platform."},
    "planner": {"plan": "starter", "name": "AI campaign planner", "shipped": True,
                "blurb": "From a brief to angles, hooks, scripts, ad variants, copy and a launch checklist."},
    "competitors": {"plan": "free", "name": "Competitor research", "shipped": True,
                    "blurb": "Research your closest competitors on the web: offers, positioning, reviews and angles to test, each with its source."},
    "adhub": {"plan": "starter", "name": "Ad-account setup hub", "shipped": True,
              "blurb": "Step-by-step setup for each ad platform, pixel basics and export specs."},
    "pages": {"plan": "starter", "name": "Landing pages", "shipped": True,
              "blurb": "Build a page from blocks and publish it on campaignify.ai."},
    "links": {"plan": "starter", "name": "Smart links", "shipped": True,
              "blurb": "Short links with click counts."},
    "flows": {"plan": "starter", "name": "Flows", "shipped": True,
              "blurb": "Build the follow-up after the ad: emails, texts, tags, waits and branches, drafted by AI from a sentence."},
    "flows_email": {"plan": "growth", "name": "Email sending in flows", "shipped": True,
                    "blurb": "Flows send their emails, with open and click tracking and your own sending domain."},
    "flows_sms": {"plan": "scale", "name": "Text messages in flows", "shipped": True,
                  "blurb": "Flows send texts to people who agreed to them, with carrier registration handled for you."},
    "seo": {"plan": "growth", "name": "SEO, AEO & GEO optimizer", "shipped": True,
            "blurb": "Audit a page for search, answer engines and AI assistants; get fixes and schema."},
    "optimizer": {"plan": "growth", "name": "Content optimizer", "shipped": True,
                  "blurb": "Score and rewrite copy for a platform: hook, clarity, CTA and compliance."},
    "link_analytics": {"plan": "growth", "name": "Smart-link analytics", "shipped": True,
                       "blurb": "Clicks over time, referrers, countries and UTM sources."},
    "forms": {"plan": "growth", "name": "Forms & contacts", "shipped": True,
              "blurb": "Forms on your pages, a contacts list with tags, and webhooks out."},
    "community": {"plan": "scale", "name": "Community & courses", "shipped": True,
                  "blurb": "Spaces, posts, courses with lessons, members and events."},
    "priority": {"plan": "scale", "name": "Priority queue", "shipped": True,
                 "blurb": "Your generations start ahead of the queue."},
}


def rank(code: str) -> int:
    return ORDER.index(code) if code in ORDER else 0


def plan_code(ws) -> str:
    """The plan in force for a workspace row (sqlite3.Row or dict)."""
    if ws is None:
        return "free"
    keys = ws.keys()
    override = ws["plan_override"] if "plan_override" in keys else None
    sub = ws["plan"] if "plan" in keys else None
    status = ws["plan_status"] if "plan_status" in keys else None
    if sub not in PLANS:
        code = "free"
    elif status in ("active", "trialing"):
        code = sub
    elif status == "past_due":
        code = "free" if renewal_paused(ws) else sub
    elif status in GRACE_STATUSES and paid_period_left(ws):
        code = sub
    else:
        code = "free"
    if override in PLANS and rank(override) > rank(code):
        code = override
    comp = ws["comp_plan"] if "comp_plan" in keys else None
    if comp in PLANS and rank(comp) > rank(code) and ws["comp_until"] and str(ws["comp_until"]) > _now_iso():
        code = comp
    return code


def _now_iso() -> str:
    # the same format as db.now(), so ISO strings compare in time order
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def plan(ws) -> dict:
    return PLANS[plan_code(ws)]


def required(feature: str) -> dict:
    return PLANS[FEATURES[feature]["plan"]]


def has(ws, feature: str) -> bool:
    return rank(plan_code(ws)) >= rank(FEATURES[feature]["plan"])


def limit(ws, key: str):
    """brands | seats | pages for this workspace's plan. None = unlimited."""
    return plan(ws)[key]


def within(ws, key: str, used: int) -> bool:
    """True while one more `key` still fits the plan."""
    cap = limit(ws, key)
    return cap is None or used < cap


def next_plan_for(key: str, used: int):
    """The cheapest paid plan that allows one more `key` (for upgrade prompts)."""
    for p in PAID:
        if p[key] is None or used < p[key]:
            return p
    return None


def plan_features(code: str, shipped_only: bool = True) -> list[dict]:
    """Features that start at exactly this plan (pricing cards say "everything
    in X, plus ...")."""
    return [dict(f, key=k) for k, f in FEATURES.items()
            if f["plan"] == code and (f["shipped"] or not shipped_only)]


def priority(ws) -> int:
    return 1 if has(ws, "priority") else 0

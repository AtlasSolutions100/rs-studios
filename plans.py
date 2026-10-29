"""Plans, what each one includes, and the one place that decides access.

Three monthly plans on Stripe (recurring Prices on the shared account), plus
the free tier everyone starts on. Every plan includes everything in the plans
below it. Credit packs (catalog.PACKS) stay on sale as top-ups for anyone.

Credits come in two buckets on a workspace:
    plan_credits  granted by each paid plan invoice; they expire at the next
                  renewal (the new month's grant replaces whatever is left)
    credits       starter credits, purchased packs and admin top-ups; these
                  never expire
Spending always takes plan credits first (db.adjust_credits).

Use `has(ws, feature)` / `limit(ws, key)` everywhere; never compare plan names
by hand. A workspace's plan is its active Stripe subscription, or an admin
"comp" plan (plan_override) when one is set.
"""
import os

# Order matters: each plan includes everything in the ones before it.
ORDER = ["free", "starter", "growth", "scale"]

PLANS = {
    "free": {"code": "free", "name": "Free", "usd": 0, "credits": 0, "price_id": None, "product_id": None,
             "brands": 1, "seats": 1, "pages": 0,
             "line": "Your starter credits and the ad studio. Top up with credit packs."},
    "starter": {"code": "starter", "name": "Starter", "usd": 39, "credits": 2000,
                "price_id": "price_1UVh8lRskSkrZOvObNaCGh46", "product_id": "prod_VWkeMnQ8iKsPWt",
                "brands": 1, "seats": 1, "pages": 1,
                "line": "For one seller running their own campaigns."},
    "growth": {"code": "growth", "name": "Growth", "usd": 99, "credits": 6000,
               "price_id": "price_1UVh8mRskSkrZOvOmLkZYfr5", "product_id": "prod_VWkeGs9w4vH9Sq",
               "brands": 3, "seats": 3, "pages": None,
               "line": "For small teams and a few brands."},
    "scale": {"code": "scale", "name": "Scale", "usd": 249, "credits": 18000,
              "price_id": "price_1UVh8nRskSkrZOvO55rLZ8lD", "product_id": "prod_VWkewxw2bUDvyd",
              "brands": None, "seats": 10, "pages": None,
              "line": "For agencies and teams running many brands."},
}
for _code, _p in PLANS.items():
    if _p["price_id"]:
        _p["price_id"] = os.environ.get(f"STRIPE_PLAN_PRICE_{_code.upper()}", _p["price_id"])
PAID = [PLANS[c] for c in ORDER if c != "free"]
BY_PRICE = {p["price_id"]: p for p in PAID}

# Subscription states that keep a plan switched on. past_due keeps access
# while Stripe retries the card; anything else drops to free.
LIVE_STATUSES = {"active", "trialing", "past_due"}

# feature -> {min plan, name, blurb, shipped}. `shipped` features are the only
# ones listed on pricing and features pages; flip it when a module lands.
FEATURES = {
    "studio": {"plan": "free", "name": "UGC ad studio", "shipped": True,
               "blurb": "Image ads, photo-to-video clips and motion copies from your product photos."},
    "brand_kit": {"plan": "free", "name": "Brand kit", "shipped": True,
                  "blurb": "Product name, approved claims and photos, so every brief starts on-brand."},
    "compliance": {"plan": "free", "name": "Compliance checks", "shipped": True,
                   "blurb": "Every brief is checked for income, health, discount and recruiting claims."},
    "copy": {"plan": "starter", "name": "Copy studio", "shipped": True,
             "blurb": "Ad copy, hooks, captions, headlines, emails and landing copy for each platform."},
    "planner": {"plan": "starter", "name": "AI campaign planner", "shipped": False,
                "blurb": "From a brief to angles, hooks, scripts, ad variants, copy and a launch checklist."},
    "adhub": {"plan": "starter", "name": "Ad-account setup hub", "shipped": False,
              "blurb": "Step-by-step setup for each ad platform, pixel basics and export specs."},
    "pages": {"plan": "starter", "name": "Landing pages", "shipped": False,
              "blurb": "Build a page from blocks and publish it on campaignify.ai."},
    "links": {"plan": "starter", "name": "Smart links", "shipped": False,
              "blurb": "Short links with click counts."},
    "seo": {"plan": "growth", "name": "SEO, AEO & GEO optimizer", "shipped": False,
            "blurb": "Audit a page for search, answer engines and AI assistants; get fixes and schema."},
    "optimizer": {"plan": "growth", "name": "Content optimizer", "shipped": True,
                  "blurb": "Score and rewrite copy for a platform: hook, clarity, CTA and compliance."},
    "link_analytics": {"plan": "growth", "name": "Smart-link analytics", "shipped": False,
                       "blurb": "Clicks over time, referrers, countries and UTM sources."},
    "forms": {"plan": "growth", "name": "Forms & contacts", "shipped": False,
              "blurb": "Forms on your pages, a contacts list with tags, and webhooks out."},
    "community": {"plan": "scale", "name": "Community & courses", "shipped": False,
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
    code = sub if sub in PLANS and status in LIVE_STATUSES else "free"
    if override in PLANS and rank(override) > rank(code):
        code = override
    return code


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

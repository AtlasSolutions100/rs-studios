"""Planner v2: the platform mix, a daily budget band and a 2-week test plan (roadmap M4.3, north star section 2).

The AI planner (planner.py) writes the angles, the creative and the copy. This module adds the part that should not
change from run to run: what to run where, how much a day, and what to do on each day of the first two weeks. It is
plain arithmetic over the brief, the product and the workspace's connected ad accounts, so the plan page, the
planner's prompt and the launch draft all say the same thing.

The two golden paths (north star):
    service  a business that sells visits, quotes or appointments (service_path.is_service) or any Leads goal:
             Google Search for people already looking, plus Meta lead ads on Campaignify's lead page.
    product  everything else: Meta Sales (Purchase on the pixel), plus Google Performance Max.
Traffic and awareness goals keep the product mix with link clicks or reach as the result.

The band. For each platform: a typical cost per click for the vertical (CPC_USD, planning assumptions, not quotes or
promises; Koby can change them), divided by a typical landing page conversion rate (CVR) gives a cost per result;
the low, suggested and high daily budgets are what LOW, MID and HIGH results over the 14-day test cost, rounded to
2 significant figures, never under the platform's floor (adspec.MIN_DAILY) and never over the plan's daily ceiling
(launches.ceiling, D3). The page says these are starting ranges from typical costs, not results.

Money: computed in US dollars, then shown in the customer's currency: the connected ad account's currency (Meta
first for the product path, Google first for the service path), else the brief's, else USD. Converted with D3's
launches.UNITS_PER_USD and kept in that currency's minor units (adspec.minor_factor), so a JPY account sees yen
with no decimals.

Connected accounts: a platform with an active connection (and AD_LAUNCH on for the workspace) is "ready": the plan
page links to Create paused campaign for it. Any other platform says so plainly and points to Connect, or to the
launch kit's by-hand steps, so nothing suggests a launch on an account that isn't connected. The brief form is
pre-filled from the connections: the currency, the platforms of the path, and a suggested daily amount.

RS_PLANNER_V2 (default on; 0 turns it off) is the kill switch.
"""
import os

import adspec

TEST_DAYS = 14
LOW, MID, HIGH = 8, 15, 30          # results over the 14-day test each band point pays for (planning assumption)
# Typical cost per click in US dollars by vertical and platform (planning assumptions for a first test; Koby's to
# tune). meta = link clicks, google_search = Search, pmax = Performance Max.
CPC_USD = {
    "general":       {"meta": 0.90, "google_search": 2.00, "pmax": 0.80},
    "local_service": {"meta": 1.20, "google_search": 5.50, "pmax": 1.10},
    "health":        {"meta": 1.30, "google_search": 3.50, "pmax": 1.00},
    "supplements":   {"meta": 1.10, "google_search": 2.00, "pmax": 0.90},
    "weight_loss":   {"meta": 1.40, "google_search": 3.00, "pmax": 1.10},
    "cosmetics":     {"meta": 1.00, "google_search": 1.80, "pmax": 0.80},
    "finance":       {"meta": 2.50, "google_search": 6.00, "pmax": 2.00},
    "credit":        {"meta": 2.50, "google_search": 6.00, "pmax": 2.00},
    "crypto":        {"meta": 2.00, "google_search": 4.00, "pmax": 1.50},
    "housing":       {"meta": 1.50, "google_search": 2.50, "pmax": 1.00},
    "employment":    {"meta": 1.20, "google_search": 2.50, "pmax": 1.00},
    "alcohol":       {"meta": 1.00, "google_search": 1.50, "pmax": 0.80},
}
# Typical share of clicks that become a result on a Campaignify landing page, by goal (planning assumption).
CVR = {"leads": 0.08, "sales": 0.025, "traffic": 1.0, "awareness": 1.0, "engagement": 1.0, "installs": 0.05}
VERTICAL_WORDS = {"general": "small brands", "local_service": "local service businesses", "health": "health",
                  "supplements": "supplements", "weight_loss": "weight loss", "cosmetics": "skin care and beauty",
                  "finance": "money and investing", "credit": "loans and credit", "crypto": "crypto",
                  "housing": "housing", "employment": "hiring", "alcohol": "alcohol"}
PLATFORM_WORDS = {"meta": "Meta", "google": "Google"}
CONNECT_WORDS = {"meta": "Meta ad account", "google": "Google Ads account"}

# The mixes: (platform, kind, what it is in plain words, share of the daily budget in %, the result it optimizes for)
MIXES = {
    "service": [("google", "google_search", "Google Search", 60,
                 "People searching for what you offer, in your area, see a text ad and land on your page."),
                ("meta", "meta", "Meta lead ads", 40,
                 "Facebook and Instagram ads that send people to your lead page with a short form.")],
    "product": [("meta", "meta", "Meta Sales", 65,
                 "Facebook and Instagram ads optimized for purchases on your pixel."),
                ("google", "pmax", "Google Performance Max", 35,
                 "One Google campaign across Search, Shopping, YouTube and Display from your images and copy.")],
}
GOAL_RESULT = {"leads": "lead", "sales": "purchase", "traffic": "link click", "awareness": "click",
               "engagement": "click", "installs": "install"}


def on() -> bool:
    return (os.environ.get("RS_PLANNER_V2") or "1").strip().lower() not in ("0", "off", "false", "no")


# ---------------------------------------------------------------- inputs

def path_of(brief: dict, service: bool = False) -> str:
    """service | product (the two golden paths)."""
    if service or brief.get("business") == "service":
        return "service"
    return "service" if (brief.get("goal") or "").lower() == "leads" else "product"


def vertical_of(brief: dict, product=None, service: bool = False) -> str:
    """The vertical for the benchmarks: the policy linter's signals on the brief and the product, else local_service
    for a service business, else general."""
    import adpolicy
    import compliance
    texts = [brief.get(k) or "" for k in ("offer", "audience", "notes")]
    cat = None
    if product is not None:
        keys = product.keys() if hasattr(product, "keys") else []
        texts += [str(product[k] or "") for k in ("name", "claims", "description", "tags") if k in keys]
        cat = adpolicy.CATEGORY_VERTICAL.get(product["category"] or "") if "category" in keys else None
    hits = adpolicy._signals(compliance.normalize(" ".join(t for t in texts if t)))
    for v in adpolicy.VERTICAL_ORDER:
        if v in hits and v in CPC_USD:
            return v
    if cat in CPC_USD:
        return cat
    return "local_service" if service or brief.get("business") == "service" else "general"


def connections(conn, ws, user=None) -> dict:
    """{platform: active connection row} the workspace can launch on (empty when AD_LAUNCH is off for it)."""
    import adplatforms
    if ws is None or not adplatforms.enabled(ws, user):
        return {}
    out = {}
    for r in conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND status='active' AND platform IN "
                          "('meta','google') ORDER BY updated_at DESC, id DESC", (int(ws["id"]),)).fetchall():
        out.setdefault(r["platform"], r)
    return out


def currency_of(path: str, conns: dict, brief: dict) -> str:
    order = ("google", "meta") if path == "service" else ("meta", "google")
    for p in order:
        c = conns.get(p)
        if c is not None and c["currency"]:
            return c["currency"].upper()
    return (brief.get("budget_currency") or "USD").upper()


# ---------------------------------------------------------------- money

def _two_sig(x: float) -> float:
    if x <= 0:
        return 0
    from math import floor, log10
    p = 10 ** (floor(log10(x)) - 1)
    return round(x / p) * p


def to_minor(usd: float, currency: str) -> int:
    """US dollars as minor units of `currency` (D3's rates), rounded to 2 significant figures in that currency."""
    import launches
    cur = (currency or "USD").upper()
    rate = launches.UNITS_PER_USD.get(cur, 1)
    return int(round(_two_sig(usd * rate) * adspec.minor_factor(cur)))


def from_brief_minor(brief: dict, currency: str) -> int | None:
    """The brief's budget per day, in minor units of `currency` (converted from the brief's own currency)."""
    import campaign_budget
    import launches
    if not campaign_budget.structured(brief):
        return None
    amount = float(brief["budget_amount"])
    if brief.get("budget_period") != "day":
        amount /= max(1, int(brief.get("budget_days") or campaign_budget.DEFAULT_DAYS))
    src = (brief.get("budget_currency") or "USD").upper()
    cur = (currency or "USD").upper()
    if src != cur:
        amount = amount / launches.UNITS_PER_USD.get(src, 1) * launches.UNITS_PER_USD.get(cur, 1)
    return int(round(amount * adspec.minor_factor(cur)))


def money(minor, currency: str) -> str:
    import launches
    return launches.money(int(minor), currency)


# ---------------------------------------------------------------- the setup

def build(brief: dict, *, service: bool = False, product=None, conns: dict | None = None, ws=None,
          operator: bool = False) -> dict:
    """The platform mix, the daily budget band per platform and in total, and the 2-week test plan."""
    import launches
    conns = conns or {}
    path = path_of(brief, service)
    goal = (brief.get("goal") or ("leads" if path == "service" else "sales")).lower()
    vertical = vertical_of(brief, product, service)
    cur = currency_of(path, conns, brief)
    cvr = CVR.get(goal, CVR["sales"] if path == "product" else CVR["leads"])
    cap = launches.ceiling(ws, cur, operator) if ws is not None else None
    rows = []
    for platform, kind, name, share, what in MIXES[path]:
        cpc = CPC_USD[vertical][kind]
        cpr = cpc / cvr
        floor_minor = to_minor(adspec.MIN_DAILY.get(platform, 100) / 100 * 5, cur)    # 5x the platform's floor
        band = []
        for n in (LOW, MID, HIGH):
            m = max(to_minor(cpr * n / TEST_DAYS, cur), floor_minor)
            band.append(m)
        conn_row = conns.get(platform)
        rows.append({"platform": platform, "kind": kind, "name": name, "share": share, "what": what,
                     "low": band[0], "mid": band[1], "high": band[2], "cpc_usd": cpc, "cpr_minor": to_minor(cpr, cur),
                     "connected": conn_row is not None,
                     "account": (conn_row["account_name"] or conn_row["external_account_id"]) if conn_row is not None
                     else None,
                     "account_currency": (conn_row["currency"] or "").upper() if conn_row is not None else None})
    total = {k: sum(r[k] for r in rows) for k in ("low", "mid", "high")}
    capped = False
    if cap:
        for k in ("low", "mid", "high"):
            if total[k] > cap:
                total[k], capped = cap, True
    mine = from_brief_minor(brief, cur)
    where = None
    if mine is not None:
        where = "below" if mine < total["low"] else ("above" if mine > total["high"] else "inside")
    return {"path": path, "goal": goal, "vertical": vertical, "vertical_words": VERTICAL_WORDS[vertical],
            "currency": cur, "rows": rows, "total": total, "cap": cap, "capped": capped, "mine": mine, "where": where,
            "result": GOAL_RESULT.get(goal, "result"), "days": TEST_DAYS, "test_plan": test_plan(path, goal),
            "connected": [r["platform"] for r in rows if r["connected"]],
            "missing": [r["platform"] for r in rows if not r["connected"]]}


def test_plan(path: str, goal: str) -> list:
    """[(days, what to do)] for the first 2 weeks, in plain words."""
    if path == "service":
        return [
            ("Days 1 to 3", "Turn both campaigns on and leave them alone. Google and Meta are learning who to show "
                            "your ads to, and changes now restart that."),
            ("Days 4 to 7", "On Google, read the search terms and add the ones that aren't customers (jobs, DIY, "
                            "free) as negative keywords. On Meta, check the leads on your forms for each ad."),
            ("Day 8", "Compare cost per lead on your forms for the 2 platforms. Move about 20% of the daily budget "
                      "to the cheaper one. Pause any ad that spent 2 times your target cost per lead with no lead."),
            ("Days 9 to 14", "Call or text every new lead within 5 minutes (your follow-up flow sends the first "
                             "message). Replace any ad marked Wearing out with a fresh one on the angle that works."),
        ]
    result = "purchase" if goal == "sales" else GOAL_RESULT.get(goal, "result")
    return [
        ("Days 1 to 3", f"Turn both campaigns on and leave them alone while they learn. Check in Meta's Events "
                        f"Manager that each {result} on your site shows up once."),
        ("Days 4 to 7", "Compare the ads' click rates. Pause any ad under half the campaign's average click rate "
                        "once it has 1,000 impressions."),
        ("Day 8", f"Compare cost per {result} for Meta and Google. Move about 20% of the daily budget to the "
                  f"cheaper one. Keep Performance Max's images and headlines that Google rates Good or Best."),
        ("Days 9 to 14", "Replace any ad marked Wearing out with a fresh one on the angle that works, and keep "
                         "the winner running. Decide the next 2 weeks' budget from cost per " + result + "."),
    ]


def prompt_lines(setup: dict) -> list:
    """What the AI planner is told, so its budget split and test plan agree with the page."""
    cur = setup["currency"]
    mix = "; ".join(f"{r['name']} {r['share']}% ({money(r['low'], cur)} to {money(r['high'], cur)} a day)"
                    for r in setup["rows"])
    lines = [f"Recommended platform mix for this {setup['path']} business: {mix}. Use these platforms and shares in "
             f"finish.budget_split unless the brief says otherwise.",
             f"Suggested daily budget for the {setup['days']}-day test: {money(setup['total']['mid'], cur)} "
             f"(range {money(setup['total']['low'], cur)} to {money(setup['total']['high'], cur)})."]
    if setup["missing"]:
        lines.append("Not connected to Campaignify yet: " + ", ".join(PLATFORM_WORDS[p] for p in setup["missing"]) +
                     ". Put connecting it (or launching it by hand) in the checklist.")
    return lines


def form_hints(conn, ws, user=None, service: bool = False, product=None) -> dict:
    """The brief form's pre-fill from the connected accounts: {"currency", "platforms", "by_goal": {goal: {"text",
    "platforms", "amount"}}}. Amounts are whole units of the currency, per day."""
    conns = connections(conn, ws, user)
    out = {"by_goal": {}, "connected": sorted(conns)}
    for goal in ("sales", "leads", "traffic", "awareness", "engagement", "installs"):
        s = build({"goal": goal, "business": "service" if service else ""}, service=service, product=product,
                  conns=conns, ws=ws)
        f = adspec.minor_factor(s["currency"])
        mix = " + ".join(r["name"] for r in s["rows"])
        out["by_goal"][goal] = {"platforms": [r["platform"] for r in s["rows"]], "amount": round(s["total"]["mid"] / f, 2),
                                "text": f"Suggested: {mix}, {money(s['total']['low'], s['currency'])} to "
                                        f"{money(s['total']['high'], s['currency'])} a day for {s['days']} days.",
                                "currency": s["currency"]}
    first = out["by_goal"]["leads" if service else "sales"]
    out["currency"] = first["currency"]
    return out

"""Planner v2 (roadmap M4.3, planner_v2.py): the platform mix, a daily budget band and a 2-week test plan.

    python tests/test_planner_v2.py      (tests/test_app.py runs it too)

  * the two golden paths: a service business (or a Leads goal) gets Google Search + Meta lead ads, a product gets
    Meta Sales + Google Performance Max; the band is low < suggested < high, rounded, over the platform floor
  * money in the customer's currency: the connected account's (JPY with no decimals, EUR), converted with D3's
    rates; capped at the plan's daily ceiling; the brief's own budget placed below, inside or above the band
  * connected accounts: the plan page says Connected for a connected platform and points to Connect (or the launch
    kit by hand) for the others; nothing offers a launch where no account is connected; AD_LAUNCH off says by hand
  * the brief form is pre-filled from the connection (currency, suggested amount per goal)
  * the planner's prompt carries the mix and band; the launch draft uses the suggested amount when the brief has no
    budget and converts the brief's currency to the account's
  * RS_PLANNER_V2=0 turns it off
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["AD_LAUNCH"] = "on"
import m4seed as S  # noqa: E402  (sets the environment first)

import campaign_budget  # noqa: E402
import db  # noqa: E402
import launches  # noqa: E402
import planner  # noqa: E402
import planner_v2 as V  # noqa: E402
from m4seed import check, ex, html, q, text  # noqa: E402

# ================================================================ 1. the two paths
print("the two golden paths")
svc = V.build({"goal": "leads", "business": "service", "offer": "AC repair"}, service=True)
prod = V.build({"goal": "sales", "offer": "Ember Candle"})
check([(r["platform"], r["kind"]) for r in svc["rows"]] == [("google", "google_search"), ("meta", "meta")]
      and svc["path"] == "service" and svc["vertical"] == "local_service", "service: Google Search, then Meta lead ads")
check([(r["platform"], r["kind"]) for r in prod["rows"]] == [("meta", "meta"), ("google", "pmax")] and prod["path"] == "product",
      "product: Meta Sales, then Google Performance Max")
check(V.build({"goal": "leads", "offer": "Free candle guide"})["path"] == "service", "any Leads goal takes the leads mix")
for s_ in (svc, prod):
    check(all(r["low"] < r["mid"] < r["high"] for r in s_["rows"]) and s_["total"]["mid"] == sum(r["mid"] for r in s_["rows"]),
          f"{s_['path']}: low < suggested < high per platform, and the total adds up")
# service Google: $8.00 a click / 7.5% = $106.67 a lead; 15 leads over 14 days = $114.29 a day -> $110 (2 significant
# figures); 8 leads -> $60.95 -> $61; 30 leads -> $228.57 -> $230
g = svc["rows"][0]
check(g["mid"] == 11000 and g["low"] == 6100 and g["high"] == 23000, "Google Search band from $8.00 a click and 7.5%: $61, $110, $230")
check(svc["currency"] == "USD" and V.money(g["mid"], "USD") == "$110", "no connection, no brief currency: US dollars")
small = V.build({"goal": "traffic", "offer": "x"})
check(all(r["low"] >= 500 for r in small["rows"]), "never under 5 times the platform's daily floor ($5)")
fin = V.build({"goal": "leads", "offer": "An investing app for beginners"})
check(fin["vertical"] == "finance" and fin["rows"][0]["cpc_usd"] == 3.4, "the vertical comes from the brief (finance)")
check(len(prod["test_plan"]) == 4 and prod["test_plan"][0][0] == "Days 1 to 3" and "search terms" in svc["test_plan"][1][1],
      "a 2-week test plan in 4 steps, per path (Search gets negative keywords)")
words = " ".join(w for _, w in svc["test_plan"] + prod["test_plan"])
check("—" not in words and "–" not in words and not re.search(r"(?i)guarantee|will get you|double your", words),
      "the test plan has no dashes and no result promises")

# ================================================================ 2. money in the customer's currency, the cap
print("currency and the cap")
owner, u = S.signup("v2-owner@example.com", plan="starter")
WS = u["workspace_id"]
CONN = S.connect(WS, u["id"], currency="JPY")
with db.connect() as conn:
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (WS,)).fetchone()
    conns = V.connections(conn, ws)
jp = V.build({"goal": "sales", "offer": "Ember Candle"}, conns=conns, ws=ws)
check(jp["currency"] == "JPY" and jp["rows"][0]["connected"] and not jp["rows"][1]["connected"],
      "a Meta account in JPY: the product path is in yen, Meta connected, Google not")
check(jp["rows"][0]["mid"] == V.to_minor(0.7 / 0.02 * 15 / 14, "JPY") and V.money(jp["rows"][0]["mid"], "JPY") == "5,600 JPY",
      "yen with no decimals: $37.50 a day is 5,600 JPY (D3's 150 a dollar, 2 significant figures)")
check(V.to_minor(40, "EUR") == 3700 and V.to_minor(40, "USD") == 4000, "EUR: $40 is 37 euros, in cents")
big = V.build({"goal": "leads", "offer": "Personal loans for bad credit", "business": "service"}, service=True, conns=conns, ws=ws)
cap = launches.ceiling(ws, "JPY")
check(big["cap"] == cap and big["total"]["high"] <= cap and big["capped"], "Starter's daily ceiling (D3, in yen) caps the band")
mine = V.build({"goal": "sales", "offer": "x", "budget_amount": 10, "budget_currency": "USD", "budget_period": "day",
                "budget_days": 14})
check(mine["where"] == "below" and mine["mine"] == 1000, "a $10 a day brief is below the band")
mid_usd = mine["total"]["mid"] / 100
check(V.build({"goal": "sales", "offer": "x", "budget_amount": mid_usd * 14, "budget_currency": "USD",
               "budget_period": "total", "budget_days": 14})["where"] == "inside", "the suggested total over 14 days is inside")
check(V.from_brief_minor({"budget_amount": 40, "budget_currency": "USD", "budget_period": "day"}, "EUR") == 3680,
      "the brief's $40 a day is 36.80 euros a day")

# ================================================================ 3. the brief form, pre-filled from the connection
print("the brief form")
r = html(owner.get("/app/campaigns"))
check('<option value="JPY" selected>JPY</option>' in r, "the currency is the connected account's (JPY)")
hint = re.search(r"data-v2-hints='([^']+)'", r)
hints = json.loads(hint.group(1).replace("&#34;", '"').replace("&quot;", '"')) if hint else {}
check(hints.get("sales", {}).get("currency") == "JPY" and hints["sales"]["platforms"] == ["meta", "google"]
      and hints["leads"]["platforms"] == ["google", "meta"], "per goal: the mix and the amount, in yen")
check("Suggested: Meta Sales + Google Performance Max" in r and "Use " in r and " JPY a day" in r,
      "the form says the suggested mix and offers Use N a day")
check(campaign_budget.parse({"budget_amount": "6000", "budget_currency": "JPY", "budget_period": "day",
                             "budget_days": "14"})[0]["budget"] == "6,000 JPY a day for 14 days (84,000 JPY total)",
      "a brief in the account's currency reads in that currency")

# ================================================================ 4. the plan page
print("the plan page")
brief = {"offer": "Ember Candle", "goal": "sales", "platforms": ["meta", "google"], "budget_amount": 3000,
         "budget_currency": "JPY", "budget_period": "day", "budget_days": 14}
final = {"summary": "Image and video first.", "budget_split": [{"platform": "meta", "share_percent": 65}],
         "kpis": ["Cost per purchase"], "test_plan": "OLD FREE TEXT", "checklist": []}
cid = ex("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, created_at, "
         "updated_at) VALUES (?,?,?,?,?,?,?,?,?)", WS, u["id"], "Ember Candle", json.dumps(brief), "ready",
         json.dumps({"angles": [], "final": final}), "[]", db.now(), db.now())
p = html(owner.get(f"/app/campaigns/{cid}"))
t = text(p)
check('data-setup="product"' in p and "Where to run it and how much" in t and "Meta Sales" in t
      and "Google Performance Max" in t, "the plan shows the product mix")
row_meta = p[p.find('data-setup-row="meta"'):p.find('data-setup-row="google"')]
row_g = p[p.find('data-setup-row="google"'):p.find('class="v2total"')]
check("Connected" in row_meta and "Ember Shop" in row_meta and "Not connected" in row_g
      and "Connect your Google Ads account" in row_g and "by hand with the kit below" in row_g,
      "Meta: Connected (Ember Shop); Google: Not connected, with Connect or the kit by hand")
check("Suggested start:" in t and " JPY a day" in t and "Your budget, 3,000 JPY a day, is under this range" in t,
      "the suggested start in yen, and the brief's 3,000 JPY a day placed under it")
check("not a forecast" in t and "OLD FREE TEXT" not in t and "The 14-day test" in t and "Days 1 to 3" in t,
      "honest about the ranges; the 14-day test replaces the free-text test plan")
os.environ["AD_LAUNCH"] = "off"
p2 = text(html(owner.get(f"/app/campaigns/{cid}")))
check("Connected" not in p2.replace("Not connected", "") and "Launch it by hand with the kit below." in p2
      and "Connect your" not in p2 and "Amounts are per day, in JPY." in p2,
      "AD_LAUNCH off: no connection counts, every platform by hand, in the brief's currency")
os.environ["AD_LAUNCH"] = "on"

# ================================================================ 5. the prompt and the launch draft
print("the prompt and the launch")
with db.connect() as conn:
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
pr = planner.brief_prompt(c, {"name": "Ember", "product": "Ember Candle", "claims": "", "audience": ""}, brief)
check("Recommended platform mix for this product business: Meta Sales 65%" in pr and "Google Performance Max 35%" in pr
      and "Not connected to Campaignify yet: Google" in pr, "the planner is told the mix, the band and what isn't connected")
setup = {"path": "product", "currency": "USD", "rows": [{"platform": "meta", "mid": 4100}, {"platform": "google", "mid": 2200}]}
check(launches._daily_from_brief({}, {"final": {"setup": setup}}, "USD", "meta") == 4100
      and launches._daily_from_brief({}, {"final": {"setup": setup}}, "EUR", "meta") == launches.DEFAULT_DAILY,
      "no budget in the brief: the launch draft takes the suggested amount (same currency only)")
check(launches._daily_from_brief({"budget_amount": 40, "budget_currency": "USD", "budget_period": "day"}, {}, "EUR", "meta")
      == 3700, "a $40 a day brief on a EUR account drafts 37 euros a day")
check(launches._daily_from_brief({"budget_amount": 40, "budget_currency": "USD", "budget_period": "day"}, {}, "USD", "meta")
      == 4000, "same currency: unchanged")

# ================================================================ 6. the kill switch
os.environ["RS_PLANNER_V2"] = "0"
check("Where to run it" not in html(owner.get(f"/app/campaigns/{cid}")) and "data-v2-hints" not in html(owner.get("/app/campaigns"))
      and "OLD FREE TEXT" in html(owner.get(f"/app/campaigns/{cid}")), "RS_PLANNER_V2=0: the plan page and form as before")
os.environ.pop("RS_PLANNER_V2")
check(q("SELECT COUNT(*) n FROM approval_cards")[0]["n"] == 0 and not S.G.calls, "nothing was drafted or sent to Meta")

print(f"\n{S.passed[0]} checks passed")

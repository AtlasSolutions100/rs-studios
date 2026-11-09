"""Creative fatigue and "next batch" suggestions (roadmap M4.2, adfatigue.py).

    python tests/test_fatigue.py      (tests/test_app.py runs it too)

  * the flag on fixtures: link click rate down 30% or more week over week at stable spend; not on a 29% drop, not
    when the spend moved more than 25%, not on a new ad, not on too few impressions or clicks the week before
  * the winning angle: the launch's best click rate this week that isn't wearing out, else the workspace's
  * the pages: the ad is tagged Wearing out, a "What to change" panel says why in plain words and links to Create;
    the list says how many; Create opens with the winning angle in the worn ad's format, behind AD_LAUNCH
  * no auto-spend: looking at the pages and the hint changes nothing on Meta and drafts no card
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m4seed as S  # noqa: E402  (sets the environment first)

import adfatigue  # noqa: E402
import db  # noqa: E402
from m4seed import check, ex, html, q, text  # noqa: E402

owner, u = S.signup("fatigue-owner@example.com")
WS = u["workspace_id"]
other, ou = S.signup("fatigue-other@example.com")
CONN = S.connect(WS, u["id"])
PLAN = S.make_plan(WS, u["id"])
LID, ADS, CAMP = S.launched(WS, u, PLAN, "Ember Candle")


def wow(this_imp, this_link, this_spend, last_imp, last_link, last_spend, this_days=7, last_days=7):
    """A week_over_week() shaped fixture."""
    def week(imp, link, spend, days):
        return {"imp": imp, "link": link, "spend": spend, "days": days,
                "ctr": round(link * 100.0 / imp, 2) if imp else None}
    t, l_ = week(this_imp, this_link, this_spend, this_days), week(last_imp, last_link, last_spend, last_days)

    def pct(a, b):
        return round((a - b) * 100.0 / b, 1) if a is not None and b else None
    return {"this": t, "last": l_, "complete": bool(this_days and last_days), "ctr_change": pct(t["ctr"], l_["ctr"]),
            "spend_change": pct(t["spend"], l_["spend"])}


# ================================================================ 1. the flag on fixtures
print("the flag")
check(adfatigue.judge(wow(10000, 120, 14000, 10000, 200, 14000)), "click rate 2.00% to 1.20% (-40%) at the same spend: flagged")
check(adfatigue.judge(wow(10000, 140, 14000, 10000, 200, 14000)), "exactly -30%: flagged")
check(not adfatigue.judge(wow(10000, 142, 14000, 10000, 200, 14000)), "-29%: not flagged")
check(adfatigue.judge(wow(10000, 120, 17000, 10000, 200, 14000)), "spend up 21%: still stable, flagged")
check(not adfatigue.judge(wow(10000, 120, 18000, 10000, 200, 14000)), "spend up 29%: not stable, not flagged")
check(not adfatigue.judge(wow(10000, 120, 9800, 10000, 200, 14000)), "spend down 30%: not flagged")
check(not adfatigue.judge(wow(10000, 120, 14000, 0, 0, 0, last_days=0)), "a new ad (nothing the week before): not flagged")
check(not adfatigue.judge(wow(500, 4, 1000, 900, 18, 1000)), "under 1,000 impressions the week before: not flagged")
check(not adfatigue.judge(wow(5000, 4, 1000, 5000, 9, 1000)), "under 10 link clicks the week before: not flagged")
check(not adfatigue.judge(wow(10000, 260, 14000, 10000, 200, 14000)), "click rate up: not flagged")

# from ad_insights_daily: ad 1 steady, ad 2 down about 40% at the same spend
for n in range(1, 15):
    this = n <= 7
    S.insight(WS, CONN, LID, ADS[0], CAMP, n, spend_minor=2000, imp=2000, link=44)
    S.insight(WS, CONN, LID, ADS[1], CAMP, n, spend_minor=2000, imp=2000, link=24 if this else 40)
with db.connect() as conn:
    L = conn.execute("SELECT * FROM launches WHERE id=?", (LID,)).fetchone()
    r = adfatigue.for_launch(conn, L)
a1, a2 = r["ads"]
check(not a1["tired"] and a2["tired"] and abs(a2["wow"]["ctr_change"] + 40.0) < 0.1 and a2["wow"]["spend_change"] == 0,
      "from Meta's daily numbers: ad 2 down 40% at the same spend is flagged, ad 1 isn't")
check(r["winner"]["id"] == ADS[0] and r["winner"]["angle"] == "Evening ritual", "the winning angle is ad 1's: Evening ritual")
check(adfatigue.words(a2).startswith("Its click rate fell 40% this week (2.00% to 1.20%) while the spend stayed about the same."),
      "the reason in plain words, with both click rates")

# ================================================================ 2. the pages
print("the pages")
graph_calls = len(S.G.calls)
cards = q("SELECT COUNT(*) n FROM approval_cards")[0]["n"]
p = html(owner.get(f"/app/ad-campaigns/{LID}"))
t = text(p)
check("Wearing out" in p and 'href="#next"' in p, "the ads table tags ad 2 Wearing out")
check("What to change" in t and "1 ad wearing out" in t and "Ad 2 Lasts the month" in t
      and "The angle working now is “Evening ritual” (Ad 1, 2.20% click rate this week)." in t,
      "the What to change panel: which ad, why, and the angle that's working")
check(f"/app/create?refresh={LID}:{ADS[1]}" in p.replace("%3A", ":"), "Refresh this ad links to Create for that ad")
lst = text(html(owner.get("/app/ad-campaigns")))
check("1 ad is getting fewer clicks at the same spend" in lst and "See what to change" in lst, "the list says 1 ad is wearing out")

c = html(owner.get(f"/app/create?refresh={LID}:{ADS[1]}"))
check("Refreshing Ad 2 Lasts the month." in c and "fell 40% this week" in c and "“Evening ritual” (2.20% click rate this week)" in c
      and "Nothing is spent or changed on Meta" in c, "Create says what it's refreshing, with the winning angle")
boot = c[c.find('"prefill"'):c.find('"prefill"') + 600]
check('"title": "Evening ritual"' in boot and '"type": "ugc"' in boot and '"hook": "Ember Candle, 50 hours"' in boot,
      "Create starts with the winning angle and its headline, in the worn ad's format (a presenter video)")
check(len(S.G.calls) == graph_calls or all(c_["method"] == "GET" for c_ in S.G.calls[graph_calls:]),
      "no auto-spend: the pages and the hint only read from Meta")
check(q("SELECT COUNT(*) n FROM approval_cards")[0]["n"] == cards, "and no change card was drafted")

c2 = html(other.get(f"/app/create?refresh={LID}:{ADS[1]}"))
check("Refreshing" not in c2, "another workspace's launch gives no hint")
check("Refreshing" not in html(owner.get(f"/app/create?refresh={LID}:nope")) and
      "Refreshing" not in html(owner.get("/app/create?refresh=abc")), "a wrong ad or a bad value is ignored")
os.environ["AD_LAUNCH"] = "off"
check("Refreshing" not in html(owner.get(f"/app/create?refresh={LID}:{ADS[1]}")), "AD_LAUNCH off: no hint")
os.environ["AD_LAUNCH"] = "on"

# ad 2 recovers: the flag goes away
for n in range(1, 8):
    S.insight(WS, CONN, LID, ADS[1], CAMP, n, spend_minor=2000, imp=2000, link=39)
check("Wearing out" not in html(owner.get(f"/app/ad-campaigns/{LID}")), "when the click rate comes back, the flag goes")

print(f"\n{S.passed[0]} checks passed")

"""Planner correctness (roadmap M5.4: UX-5 to UX-9, COPY-1), on a throwaway database:

  * UX-6: the budget is a number with a currency and a period; the plan says "$600 total over 14 days" and splits it
    in money next to the percentages, in the header, the budget split and the launch kit; a bad number is refused
    before anything is planned; a plan made with the old free-text budget still shows it as typed
  * UX-7: the platform chips start from the goal (Meta and Google for sales, Google and Meta for leads), TikTok is
    optional, and the page carries the goal map for the chips to follow
  * UX-5: a blocked copy variant has no Copy buttons and one "Rewrite this one" (the optimizer on Growth, the plan's
    Revise box on Starter); passing variants keep theirs
  * UX-8 / UX-9: one balance, written with the same words as the pill; thousands separators on every credit amount
  * COPY-1: the approved claims are a list, not one run-on line

    python tests/test_planner_polish.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_plannerpolish_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  ANTHROPIC_API_KEY="sk-ant-test", RS_LAUNCH_KIT="1")

import app as appmod  # noqa: E402
import campaign_budget as cb  # noqa: E402
import db  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.13.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email, plan="starter"):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
    return c, u


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


BLOCKED = {"angle": "Evening ritual", "ok": False,
           "compliance": {"ok": False, "warnings": [], "blocks": ["No discounts, coupons or free-shipping offers."]},
           "fields": [{"name": "Primary text", "text": "Free shipping over $50."}]}
PASSING = {"angle": "Lasts the month", "ok": True, "compliance": {"ok": True, "warnings": [], "blocks": []},
           "fields": [{"name": "Primary text", "text": "One candle, fifty hours."}]}
PLAN = {"angles": [{"name": "Evening ritual", "insight": "x", "audience": "Renters"}],
        "copy": [{"platform": "meta", "kind": "ad", "angle": "all", "variants": [BLOCKED, PASSING]}],
        "final": {"summary": "Image ads first.",
                  "budget_split": [{"platform": "meta", "share_percent": 60, "note": "Image ads first"},
                                   {"platform": "google", "share_percent": 40}],
                  "checklist": [{"title": "Upload the 2 image ads", "platform": "meta"}]}}


def campaign(ws, uid, brief):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ws, uid, "Ember Candle", json.dumps(brief), "ready", json.dumps(PLAN), "[]", db.now(),
                            db.now())).lastrowid
        conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?,?)", (cid, ws, "Lasts the month", "tiktok", "ugc_video", "b",
                                                                1387, "proposed", db.now()))
        conn.commit()
    return cid


try:
    print("== UX-6: the budget as money")
    f = {"budget_amount": "600", "budget_currency": "USD", "budget_period": "total", "budget_days": "14"}
    got, err = cb.parse(f)
    check(err is None and got["budget_amount"] == 600 and got["budget"] == "$600 total over 14 days", "600 total, 14 days")
    got, _ = cb.parse(dict(f, budget_amount="1,250.5", budget_currency="EUR", budget_period="day", budget_days="10"))
    check(got["budget"] == "€1,250.50 a day for 10 days (€12,505 total)", "per day, euros, thousands separators, cents")
    check(cb.share(got, 60) == "€750.30 a day", "a per-day budget splits per day")
    check(cb.share({"budget": "$600 for two weeks"}, 60) is None and cb.text({"budget": "600"}) == "600",
          "an old free-text budget has no money split and shows as typed")
    for bad, why in (("abc", "a word"), ("0", "zero"), ("-5", "negative"), ("1e12", "too big"), ("nan", "not a number")):
        check(cb.parse(dict(f, budget_amount=bad))[1], f"refused: {why}")
    check(cb.parse(dict(f, budget_days="0"))[1] and cb.parse(dict(f, budget_days="x"))[1], "the test length must be 1 to 365 days")
    check(cb.parse(dict(f, budget_amount=""))== ({}, None), "no amount, no budget (the planner assumes a small test)")
    check(cb.parse(dict(f, budget_currency="XYZ", budget_period="week"))[0]["budget"] == "$600 total over 14 days",
          "unknown currency or period fall back to USD and total")

    c, u = signup("owner@example.com")
    ws = u["workspace_id"]
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET credits=4090, claims=? WHERE id=?",
                     ("50 hour burn time\nHand poured in small batches\nSoy wax", ws))
        wsr = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws,)).fetchone()
        bid = wsr["brand_id"] or conn.execute("INSERT INTO brands (workspace_id, brand_name, created_at) VALUES (?,?,?)",
                                              (ws, "Hearth & Co", db.now())).lastrowid
        conn.execute("UPDATE workspaces SET brand_id=? WHERE id=?", (bid, ws))
        prod = conn.execute("INSERT INTO products (workspace_id, brand_id, name, claims, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?)", (ws, bid, "Ember Candle",
                                                     "50 hour burn time\nHand poured in small batches\n\nSoy wax ",
                                                     db.now(), db.now())).lastrowid
        conn.execute("UPDATE workspaces SET product_id=? WHERE id=?", (prod, ws))
        conn.commit()
        n0 = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    r = c.post("/app/campaigns", data={"csrf": csrf(c), "offer": "Ember Candle", "goal": "sales", "budget_amount": "lots",
                                       "budget_period": "total", "budget_days": "14", "platforms": ["meta"]},
               follow_redirects=True)
    with db.connect() as conn:
        n1 = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    check(n1 == n0 and "Budget: enter a number, like 300." in r.get_data(as_text=True),
          "a budget that isn't a number is refused before anything is planned or charged")

    print("== the planner form")
    html = c.get("/app/campaigns").get_data(as_text=True)
    check('type="number" name="budget_amount"' in html and 'name="budget_currency"' in html
          and '<option value="total">Total for the test</option>' in html and '<option value="day">Per day</option>' in html
          and 'name="budget_days"' in html, "amount, currency, total or per day, and the test length")
    chips = dict(re.findall(r'<input type="checkbox" name="platforms" value="(\w+)" ([^>]*)>', html))
    check(chips["meta"].startswith("checked") and chips["google"].startswith("checked") and not chips["tiktok"].startswith("checked"),
          "UX-7: sales starts on Meta and Google; TikTok is optional")
    gmap = json.loads(re.search(r"data-goal-platforms='([^']+)'", html).group(1).replace("&#34;", '"'))
    check(gmap["leads"] == ["google", "meta"] and gmap["sales"] == ["meta", "google"], "the chips follow the goal (leads: Google, Meta)")
    check("data-goal-platforms" in html and "<title>Campaigns · Campaignify</title>" in html,
          "the scripts sit in the page, not inside the title")
    claims = html.split('class="claimlist', 1)[1].split("</ul>", 1)[0]
    check(re.findall(r"<li>([^<]+)</li>", claims) == ["50 hour burn time", "Hand poured in small batches", "Soy wax"],
          "COPY-1: the approved claims are a list")
    check("You have 4,090 credits." in html, "UX-8/9: the balance with the pill's words and a thousands separator")

    print("== the plan page")
    cid = campaign(ws, u["id"], {"offer": "Ember Candle", "goal": "sales", "platforms": ["meta", "google"],
                                 **cb.parse(f)[0]})
    html = c.get(f"/app/campaigns/{cid}").get_data(as_text=True)
    head = text(html.split("<h1>", 1)[1].split("</p>", 1)[0])
    check("Ad budget $600 total over 14 days" in head and "Budget 600" not in head, "UX-6: the header says money and period")
    split = text(html.split("Budget split for the first test", 1)[1].split("</div></div>", 1)[0])
    check("$360 · 60%" in split and "$240 · 40%" in text(html.split("Budget split for the first test", 1)[1][:1500]),
          "the split in dollars next to the percentages")
    check('<div class="adcost"><b>1,387</b><span class="small muted">credits</span></div>' in html, "UX-9: 1,387 credits")
    check("Selected: 0 ads · 0 credits" in html and "you have <span data-plan-bal>4,090 credits</span>" in html,
          "one balance, worded like the pill")
    check("toLocaleString('en-US')" in html and "(n === 1 ? '' : 's')" in html, "the live total keeps the separators and the plural")
    kit = text(html.split('id="kit-meta"', 1)[1].split('id="kit-google"', 1)[0])
    check("Budget $360 (60% of $600 total over 14 days). Image ads first." in kit, "the launch kit's budget line in money")

    print("== UX-5: a blocked variant can't be copied")
    copy = html.split("Copy for each platform", 1)[1].split("</section>", 1)[0]
    bl = copy.split('class="variant flagged"', 1)[1].split("</article>", 1)[0]
    ok = copy.split("Lasts the month", 1)[1].split("</article>", 1)[0]
    check("data-copy=" not in bl and "Free shipping over $50." in bl, "the blocked text shows, with no Copy button")
    check('href="#revise" data-revise-note="Rewrite the Evening ritual copy so it passes the checks: No discounts' in bl
          and bl.count("Rewrite this one") == 1, "Starter: one Rewrite this one, into the plan's Revise box")
    check("data-copy=" in ok and "Rewrite this one" not in ok, "a passing variant keeps its Copy button")
    check('id="revise"' in html and 'id="revise-notes"' in html, "the Revise box it fills")
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (ws,))
        conn.commit()
    html = c.get(f"/app/campaigns/{cid}").get_data(as_text=True)
    bl = html.split('class="variant flagged"', 1)[1].split("</article>", 1)[0]
    check('href="/app/optimize?text=Free+shipping+over+$50.&amp;platform=meta">Rewrite this one</a>' in bl,
          "Growth: Rewrite this one opens the optimizer with the text and platform")

    print("== an old plan with a free-text budget")
    cid2 = campaign(ws, u["id"], {"offer": "Ember Candle", "goal": "sales", "budget": "$600 for two weeks",
                                  "platforms": ["meta"]})
    html = c.get(f"/app/campaigns/{cid2}").get_data(as_text=True)
    check("Ad budget $600 for two weeks" in text(html) and "60% of $600 for two weeks" in text(html),
          "shows as typed, split by percentage only")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} checks passed")

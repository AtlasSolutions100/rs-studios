"""The results screen in the new look (results.py, web/v2/results.html), on m4seed's throwaway database with a fake
Meta. Every number on the page is seeded here and checked to the unit.

  * new look off: no page and no menu link; on: the link sits between Leads and Brand
  * an empty workspace: zeros and plain words saying what to make or connect, nothing else
  * Campaignify's own counts by range (7, 30, 90 days, the edges included): a person who sends twice is one lead;
    link clicks; ads made and how many of those are approved; where the leads came from; the links clicked most
  * per campaign (a desk conversation): its ads, its page with views as all time, the leads of its form
  * ad accounts: off for the workspace, none connected, nothing launched, then real spend per campaign and per ad,
    the leads on the member's own forms and what each cost, totals, and what stands out (the best ad, a worn ad, a
    campaign that spent with nothing to show, a change waiting for approval); leads from ads not launched here
  * another workspace's numbers never show
  * the page reads only: no form, no button, and none of the words that promise a result

    python tests/test_results.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m4seed as S  # noqa: E402  (sets the environment first)

for k in ("UI_V2", "HOME_V2", "PLANS_ONLY"):
    os.environ.pop(k, None)
import db  # noqa: E402
import results  # noqa: E402
from m4seed import check, ex, html  # noqa: E402


def text(fragment):
    return htmllib.unescape(S.text(fragment))


def page(c, rng=None):
    r = c.get("/app/results" + (f"?range={rng}" if rng else ""))
    assert r.status_code == 200, r.status_code
    return html(r)


def main_of(body):
    return body[body.index('data-results'):body.index('rs-foot')]


def nums(body):
    """{label: value} of the band of figures."""
    band = body[body.index("data-rs-nums"):body.index("</dl>", body.index("data-rs-nums"))]
    return {text(a): text(b) for a, b in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", band, re.S)}


def at(n, hour=18):
    """A time on the ad account's day n days ago (18:00 UTC is midday in Chicago)."""
    return f"{S.day(n)}T{hour:02d}:00:00+00:00"


def lead(ws_id, form_id, email, n, *, utm=None, link=None, slug=None, platform=None, launch=None, ad=None, camp=None):
    """One sent form n days ago, with its contact, and its ad source when it came from an ad."""
    with db.connect() as conn:
        c = conn.execute("SELECT id FROM contacts WHERE workspace_id=? AND email=?", (ws_id, email)).fetchone()
        cid = c["id"] if c else conn.execute(
            "INSERT INTO contacts (workspace_id, email, first_seen, last_seen) VALUES (?,?,?,?)",
            (ws_id, email, at(n), at(n))).lastrowid
        sid = conn.execute("INSERT INTO form_submissions (form_id, workspace_id, contact_id, data_json, page_slug, link_code, "
                           "utm_source, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (form_id, ws_id, cid, "{}", slug, link, utm, at(n))).lastrowid
        if platform:
            conn.execute("INSERT INTO lead_sources (workspace_id, contact_id, submission_id, form_id, platform, launch_id, "
                         "campaign_external_id, ad_external_id, matched, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (ws_id, cid, sid, form_id, platform, launch, camp, ad,
                          "ad" if ad else ("campaign" if launch else "platform"), at(n)))
        conn.commit()
    return sid


def form(ws_id, code, name):
    return ex("INSERT INTO forms (workspace_id, code, name, fields_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
              ws_id, code, name, "[]", db.now(), db.now())


def job(ws_id, user_id, n, *, tool="ad_image", status="done", batch=None, verdicts=()):
    jid = ex("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, updated_at, "
             "batch_id) VALUES (?,?,?,?,?,?,?,?,?,?)", ws_id, user_id, tool, "b", "{}", status, 12, at(n), at(n), batch)
    for i, v in enumerate(verdicts):
        ex("INSERT INTO job_reviews (job_id, user_id, workspace_id, verdict, at) VALUES (?,?,?,?,?)", jid, user_id + i, ws_id,
           v, at(n))
    return jid


# ================================================================ 1. off and on
print("the new look off, then on")
a, u = S.signup("results-a@example.com")
WS = u["workspace_id"]
ex("UPDATE workspaces SET name='Ember Candle Co' WHERE id=?", WS)
r = a.get("/app/results")
check(r.status_code == 302 and "/app/results" not in r.headers["Location"], "new look off: no results page")
check("<span>Results</span>" not in html(a.get("/app/library")), "and no link in the first look's menu")
os.environ["UI_V2"] = "1"
body = page(a)
side = re.search(r'<aside class="side".*?</aside>', body, re.S).group(0)
check(side.index("<span>Library</span>") < side.index("<span>Results</span>") < side.index("<span>Brand</span>"),
      "new look on: Results is in the menu before Brand")
check('href="/app/results" aria-current=page' in side, "and marked as the page you are on")

# ================================================================ 2. empty
print("an empty workspace")
os.environ["AD_LAUNCH"] = "off"
body = page(a)
main = text(main_of(body))
check(nums(body) == {"Leads": "0", "Link clicks": "0", "Ads made": "0"}, "three zeros and no ad figures")
check("No campaigns yet." in main and "No leads in these 7 days." in main and "No clicks in these 7 days." in main,
      "each part says in plain words that there is nothing yet")
check("Campaignify does not read ad accounts for this workspace" in main and 'data-rs-ads="off"' in body
      and "rs-accts" not in main_of(body), "ad accounts off for the workspace: said so, no account chips")
check("with the spend your ad accounts report" not in main, "and the headline does not mention ad spend")
os.environ["AD_LAUNCH"] = "on"
body = page(a)
check('data-rs-ads="connect"' in body and "No ad account is connected." in text(body) and "Connect an ad account" in body,
      "ad accounts on, none connected: connect one")
check(all(f'href="/app/results?range={k}"' in body for k in ("7", "30", "90")) and
      re.search(r'range=7" aria-current=true', body), "the three ranges, 7 days first")
check(re.search(r'range=7" aria-current=true', page(a, "365")) and "these 7 days" in text(page(a, "abc")),
      "an unknown range reads as 7 days")

# ================================================================ 3. Campaignify's own counts
print("own counts by range")
F1 = form(WS, "resq1", "Quote")
lead(WS, F1, "ann@example.com", 2, utm="newsletter")
lead(WS, F1, "ann@example.com", 1, utm="newsletter")          # the same person again: still one lead
lead(WS, F1, "bo@example.com", 6, link="bio")                 # the first day of the 7
lead(WS, F1, "cy@example.com", 7)                             # one day too old for 7
lead(WS, F1, "di@example.com", 29)                            # the first day of the 30
lead(WS, F1, "ed@example.com", 60)
lead(WS, F1, "flo@example.com", 95)                           # older than every range
LK = ex("INSERT INTO links (workspace_id, code, title, target_url, created_at) VALUES (?,?,?,?,?)", WS, "bio", "Bio link",
        "https://example.com", db.now())
LK2 = ex("INSERT INTO links (workspace_id, code, target_url, created_at) VALUES (?,?,?,?)", WS, "promo", "https://example.com",
         db.now())
for n in (0, 1, 3, 12, 40):
    ex("INSERT INTO link_clicks (link_id, at) VALUES (?,?)", LK, at(n))
ex("INSERT INTO link_clicks (link_id, at) VALUES (?,?)", LK2, at(2))
job(WS, u["id"], 1, verdicts=("approved",))
job(WS, u["id"], 2, tool="ugc_video", verdicts=("approved", "rejected"))     # turned down by someone: not approved
job(WS, u["id"], 3, status="failed")
job(WS, u["id"], 3, status="running")
job(WS, u["id"], 20, verdicts=("approved",))
ex("UPDATE jobs SET deleted_at=? WHERE id=?", db.now(), job(WS, u["id"], 1))   # a deleted ad is not counted

b7, b30, b90 = page(a), page(a, "30"), page(a, "90")
check(nums(b7) == {"Leads": "2", "Link clicks": "4", "Ads made": "2"},
      "7 days: 2 people (one sent twice), 4 clicks, 2 finished ads")
check("1 approved" in text(b7), "of the 2 ads, the one nobody turned down is approved")
check(nums(b30)["Leads"] == "4" and nums(b30)["Link clicks"] == "5" and nums(b30)["Ads made"] == "3",
      "30 days: the day-7 and day-29 leads, the day-12 click, the day-20 ad")
check(nums(b90)["Leads"] == "5" and nums(b90)["Link clicks"] == "6", "90 days: the day-60 lead and the day-40 click; day 95 is out")
src = re.findall(r"<li><span>(.*?)</span><b>(\d+)</b></li>", b7[b7.index("data-rs-sources"):b7.index("data-rs-links")])
check(src == [("newsletter", "1"), ("Smart links", "1")], "7 days: one lead tagged newsletter, one from a smart link")
src30 = dict(re.findall(r"<li><span>(.*?)</span><b>(\d+)</b></li>", b30[b30.index("data-rs-sources"):b30.index("data-rs-links")]))
check(src30 == {"newsletter": "1", "Smart links": "1", "Not tagged": "2"} and list(src30)[-1] == "Not tagged",
      "30 days: the two leads with no source are not tagged, listed last")
lk = re.findall(r"<li><span>(.*?)</span><b>(\d+)</b></li>", b7[b7.index("data-rs-links"):])
check(lk == [("Bio link", "3"), ("/l/promo", "1")], "the links clicked most, by name, else by address")

# ================================================================ 4. per campaign
print("per campaign")
F2 = form(WS, "resq2", "Winter sale sign-up")
PG = ex("INSERT INTO pages (workspace_id, slug, title, description, status, blocks_json, views, created_at, updated_at) VALUES "
        "(?,?,?,?,?,?,?,?,?)", WS, "winter-sale", "Winter sale", "", "published", "[]", 1234, db.now(), db.now())
BATCH = ex("INSERT INTO batches (workspace_id, user_id, ad_type, title, created_at) VALUES (?,?,?,?,?)", WS, u["id"], "image", "Winter",
           db.now())
job(WS, u["id"], 40, batch=BATCH, verdicts=("approved",))
job(WS, u["id"], 40, batch=BATCH)
job(WS, u["id"], 40, batch=BATCH, status="running")
T1 = ex("INSERT INTO desk_threads (workspace_id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)", WS, u["id"],
        "Winter sale", db.now(), db.now())
made = [{"kind": "ads", "name": "3 ads", "state": "Being made", "href": f"/app/studio?made={BATCH}"},
        {"kind": "page", "name": "Page: Winter sale", "state": "Draft", "href": f"/app/pages/{PG}"},
        {"kind": "form", "name": "Sign-up form", "state": "Ready", "href": f"/app/forms/{F2}"}]
ex("INSERT INTO desk_turns (thread_id, workspace_id, role, kind, body_json, created_at) VALUES (?,?,?,?,?,?)", T1, WS, "desk",
   "made", json.dumps({"made": made, "batch": BATCH, "credits": 36}), db.now())
T2 = ex("INSERT INTO desk_threads (workspace_id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)", WS, u["id"],
        "Spring teaser", at(3), at(3))
lead(WS, F2, "gil@example.com", 2)
lead(WS, F2, "hana@example.com", 3)
lead(WS, F2, "hana@example.com", 3)
lead(WS, F1, "ivy@example.com", 4, slug="winter-sale")        # another form, sent on the campaign's page
lead(WS, F2, "jo@example.com", 15)
b7 = page(a)
tbl = b7[b7.index("data-rs-camps"):b7.index("</table>", b7.index("data-rs-camps"))]
rows = [text(x) for x in re.findall(r"<tr>(.*?)</tr>", tbl.split("<tbody>")[1], re.S)]
check(rows[0] == "Winter sale 2 and 1 being made 1 Winter sale 1,234 views, all time 3",
      "the campaign: 2 ads made, 1 being made, 1 approved, its page's views as all time, 3 leads in 7 days")
check(rows[1] == "Spring teaser 0 0 No page No form", "a campaign with no page or form says so, not zero leads")
check(f'href="/app/desk/c/{T1}"' in tbl and f'href="/app/pages/{PG}"' in tbl, "each links to its conversation and its page")
b30 = page(a, "30")
check(" 4" in text(re.findall(r"<tr>(.*?)</tr>", b30[b30.index("data-rs-camps"):].split("<tbody>")[1], re.S)[0])[-3:],
      "30 days: the day-15 lead too")
ex("UPDATE pages SET status='draft' WHERE id=?", PG)
check("Winter sale not published yet" in text(page(a)), "a page that is not live shows no views")
ex("UPDATE pages SET status='published' WHERE id=?", PG)

# ================================================================ 5. ad accounts
print("ad accounts")
CONN = S.connect(WS, u["id"])
body = page(a)
check('data-rs-ads="launch"' in body and "Meta, connected" in text(body) and "Nothing has been launched from Campaignify yet."
      in text(body), "connected, nothing launched: said so, with the account's chip")
LID, ADS, CAMP = S.launched(WS, u, S.make_plan(WS, u["id"]), "Ember Candle")
LID2, ADS2, CAMP2 = S.launched(WS, u, S.make_plan(WS, u["id"], "Gift set"), "Gift set")
body = page(a)
check('data-rs-ads="quiet"' in body and "None of your launched campaigns delivered in these 7 days." in text(body),
      "launched, no delivery yet: said so")
for n in range(1, 15):
    S.insight(WS, CONN, LID, ADS[0], CAMP, n, spend_minor=2000, imp=2000, link=44, results=1 if n % 2 else 0,
              leads=1 if n % 2 else 0)
    S.insight(WS, CONN, LID, ADS[1], CAMP, n, spend_minor=2000, imp=2000, link=24 if n <= 7 else 40)
for n in range(1, 8):
    S.insight(WS, CONN, LID2, ADS2[0], CAMP2, n, spend_minor=500, imp=900, link=5)
S.insight(WS, CONN, LID, ADS[0], CAMP, 20, spend_minor=1000, imp=500, link=3)
for i in range(4):
    lead(WS, F1, f"ad{i}@example.com", 2, platform="meta", launch=LID, ad=ADS[0], camp=CAMP)
body = page(a)
main = text(main_of(body))
n7 = nums(body)
check(n7["Spent on ads"] == "$270" and n7["Cost per lead"] == "$67.50" and "4 leads from ads on your forms" in main,
      "totals: $120 + $120 + $30 spent in the 7 days, 4 form leads from ads, $67.50 each")
check(n7["Leads"] == "9", "the ad leads are leads too (2 + 3 on the campaign's page and form + 4 from ads)")
ads_tbl = body[body.index('data-rs-ads="rows"'):body.index("</table>", body.index('data-rs-ads="rows"'))]
rows = [text(x) for x in re.findall(r"<tr[^>]*>(.*?)</tr>", ads_tbl.split("</thead>")[1], re.S)]
check(rows[0] == "Gift set Meta $30 None 0 No leads Paused", "newest campaign first: Gift set spent $30, no leads either way")
check(rows[1] == "Evening ritual $30 None 0 No leads Paused" and rows[2].startswith("Lasts the month No delivery"),
      "its ads under it, the one that never ran as no delivery")
check(rows[3] == "Ember Candle Meta $240 3 leads 4 $60 Paused", "Ember Candle: $240, 3 leads as Meta counts them, 4 on the forms at $60")
check(rows[4] == "Evening ritual $120 3 leads 4 $30 Paused", "its first ad: $120, 4 form leads at $30 each")
check(rows[5] == "Lasts the month $120 None 0 No leads Wearing out",
      "its second ad: no leads, and wearing out (its click rate fell 40% at the same spend)")
check(f'href="/app/ad-campaigns/{LID}"' in ads_tbl, "a campaign opens its own page")
read = [(k, text(x)) for k, x in re.findall(r'<li data-read="(\w+)">(.*?)</li>', body, re.S)]
kinds = [k for k, _ in read]
check(kinds == ["best", "tired", "none"], "what stands out: the best ad, the worn ad, the campaign with nothing to show")
check(read[0][1] == "Best Evening ritual in Ember Candle is the only ad that brought leads on your forms: 4 leads at $30 each. "
      "Open the campaign", "the best ad, as a fact")
check(read[1][1].startswith("Wearing out Lasts the month in Ember Candle. Its click rate fell 40% this week (2.00% to 1.20%)")
      and f'/app/create?refresh={LID}:{ADS[1]}' in body and read[1][1].endswith("Make a fresh version"),
      "the worn ad, in the fatigue rule's own words, with the way to refresh it")
check(read[2][1] == "No leads Gift set spent $30 in these 7 days and brought no leads, by Meta's count or on your forms. "
      "Open the campaign", "the campaign that spent with nothing to show")
lead(WS, F1, "ad9@example.com", 3, platform="meta", launch=LID, ad=ADS[1], camp=CAMP)
body = page(a)
read = [text(x) for _, x in re.findall(r'<li data-read="(\w+)">(.*?)</li>', body, re.S)]
check(read[0].startswith("Best Evening ritual in Ember Candle brought 4 of the 5 leads your ads sent to your forms, at $30 each. "
                         "That is the lowest cost per lead of your ads."), "two ads with leads: the cheaper one, and its share")
check(nums(body)["Cost per lead"] == "$54", "totals follow: $270 over 5 leads")
ex("INSERT INTO approval_cards (workspace_id, launch_id, kind, connection_id, after_json, created_by, created_at) VALUES "
   "(?,?,?,?,?,?,?)", WS, LID2, "status_change", CONN, json.dumps({"title": "Pause Gift set"}), u["id"], db.now())
body = page(a)
read = [(k, text(x)) for k, x in re.findall(r'<li data-read="(\w+)">(.*?)</li>', body, re.S)]
check(read[-1] == ("waiting", "Waiting A change to Gift set is waiting for approval: Pause Gift set. Check the card")
      and f'href="/app/ad-campaigns/{LID2}#card"' in body, "a change waiting for approval links to its card")
ex("DELETE FROM approval_cards WHERE launch_id=? AND kind='status_change'", LID2)
b30 = page(a, "30")
check(nums(b30)["Spent on ads"] == "$605" and "Ember Candle Meta $570" in text(b30),
      "30 days: every stored day ($560 + the $10 of day 20 + $35)")
lead(WS, F1, "tt@example.com", 1, platform="tiktok")
body = page(a)
check("1 lead came from TikTok ads that were not launched from Campaignify." in text(body),
      "a lead from an ad started elsewhere is named, with no spend made up for it")
check("TikTok ads" in dict(re.findall(r"<li><span>(.*?)</span><b>(\d+)</b></li>",
                                      body[body.index("data-rs-sources"):body.index("data-rs-links")])),
      "and it is a source of leads")
ex("UPDATE ad_connections SET status='expired' WHERE id=?", CONN)
check("Meta, needs reconnecting" in text(page(a)), "an expired connection says it needs reconnecting")
ex("UPDATE ad_connections SET status='active' WHERE id=?", CONN)

# ================================================================ 6. another workspace
print("another workspace")
b, ub = S.signup("results-b@example.com")
WSB = ub["workspace_id"]
FB = form(WSB, "zetaq", "Zeta quote")
for i in range(11):
    lead(WSB, FB, f"zeta{i}@example.com", 1, utm="zetasource")
LKB = ex("INSERT INTO links (workspace_id, code, title, target_url, created_at) VALUES (?,?,?,?,?)", WSB, "zeta", "Zeta link",
         "https://example.com", db.now())
for _ in range(17):
    ex("INSERT INTO link_clicks (link_id, at) VALUES (?,?)", LKB, at(1))
TB = ex("INSERT INTO desk_threads (workspace_id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)", WSB, ub["id"],
        "Zeta soap launch", db.now(), db.now())
# B's campaign points at A's page, form and batch: it must not read them
ex("INSERT INTO desk_turns (thread_id, workspace_id, role, kind, body_json, created_at) VALUES (?,?,?,?,?,?)", TB, WSB, "desk",
   "made", json.dumps({"made": made, "batch": BATCH}), db.now())
CONNB = S.connect(WSB, ub["id"])
LIDB, ADSB, CAMPB = S.launched(WSB, ub, S.make_plan(WSB, ub["id"], "Zeta Soap"), "Zeta Soap")
for n in range(1, 5):
    S.insight(WSB, CONNB, LIDB, ADSB[0], CAMPB, n, spend_minor=7700, imp=3000, link=50)
before = nums(page(a))
ba = page(a)
check(not re.search(r"(?i)zeta", main_of(ba)), "A's page names nothing of B's: no campaign, link, source or ad")
check(nums(ba) == before and nums(ba)["Spent on ads"] == "$270", "and A's figures are A's alone")
bb = page(b)
mb = text(main_of(bb))
check(nums(bb) == {"Leads": "11", "Link clicks": "17", "Ads made": "2", "Spent on ads": "$308", "Leads from ads": "0"},
      f"B's page has B's figures alone (its plan's 2 ads; no cost per lead without a lead from an ad): {nums(bb)}")
check("Zeta soap launch 0 0 No page No form" in mb, "B's campaign reads nothing from A's page, form or ads")
check(not re.search(r"Ember|Winter|Gift set|newsletter|Bio link", main_of(bb)), "and B's page names nothing of A's")
check(a.get(f"/app/desk/c/{TB}").status_code == 302, "(the other workspace's campaign is not there for A)")

# ================================================================ 7. it only reads
print("reads only, promises nothing")
m = main_of(ba)
check("<form" not in m and "<button" not in m, "no form and no button: the page changes nothing")
check(not re.search(r"(?i)guarantee|you will get|will bring|expect|projected|forecast|benchmark|industry average|on track", text(m)),
      "no promise, projection or comparison with others")
check("—" not in m and "–" not in m, "no dashes")
with db.connect() as conn:
    cards = conn.execute("SELECT COUNT(*) FROM approval_cards").fetchone()[0]
    page(a, "90")
    check(conn.execute("SELECT COUNT(*) FROM approval_cards").fetchone()[0] == cards, "opening it makes no card")
    check(results.window(results._tz(conn, S.ws(WS)), 7)[0].isoformat() == S.day(6), "the range is today and the 6 days before")

print(f"\nresults: {S.passed[0]} checks passed")

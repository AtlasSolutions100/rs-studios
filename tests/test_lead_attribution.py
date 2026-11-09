"""Leads to contacts with source and cost (roadmap M4.1, adleads.py): which ad brought which lead, at what cost.

    python tests/test_lead_attribution.py      (tests/test_app.py runs it too)

  * a page opened from a Meta ad (Campaignify's url tags: utm_campaign = campaign id, utm_content = ad id, plus the
    fbclid) carries the tags and the click id in its form; the hosted form does too
  * the sent form keeps one lead_sources row: the tags, the click id, the platform, the ad and its launch; the
    contact's page says where they came from; the contacts list tags them; the CSV and the webhook carry it
  * matching only on this workspace's launched objects: campaign only, platform only, someone else's ad id
  * flows still start on the new lead, exactly as before
  * the Ad campaigns pages: leads per ad from our forms next to Meta's numbers, cost per lead = Meta's spend over
    our leads, in the range and the ad account's time zone; one person who sends twice is one lead
  * measurement off keeps the tags but not the click id; deleting the contact deletes its source rows
"""
import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m4seed as S  # noqa: E402  (sets the environment first)

import adleads  # noqa: E402
import db  # noqa: E402
import flows  # noqa: E402
import forms  # noqa: E402
from m4seed import check, ex, html, q, text  # noqa: E402

owner, u = S.signup("leads-owner@example.com")
WS = u["workspace_id"]
other, ou = S.signup("leads-other@example.com")
CONN = S.connect(WS, u["id"])
PLAN = S.make_plan(WS, u["id"])
LID, ADS, CAMP = S.launched(WS, u, PLAN, "Ember Candle")
LID2, ADS2, CAMP2 = S.launched(WS, u, S.make_plan(WS, u["id"], "Gift set"), "Gift set")
SECRET = S.appmod.app.secret_key

FIELDS = [{"key": "name", "label": "Name", "type": "text", "required": False, "options": [], "show_if": None},
          {"key": "email", "label": "Email", "type": "email", "required": True, "options": [], "show_if": None},
          {"key": "phone", "label": "Phone", "type": "phone", "required": False, "options": [], "show_if": None}]
FORM = ex("INSERT INTO forms (workspace_id, code, name, fields_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
          WS, "emberq", "Get a quote", json.dumps(FIELDS), db.now(), db.now())
ex("INSERT INTO pages (workspace_id, slug, title, description, status, blocks_json, created_at, updated_at) "
   "VALUES (?,?,?,?,?,?,?,?)", WS, "ember", "Ember", "", "published",
   json.dumps([{"type": "hero", "headline": "Ember candles", "subhead": "", "button_text": "", "button_url": "", "media": None},
               {"type": "form", "heading": "Get a quote", "form": FORM}]), db.now(), db.now())
# a flow on any form lead (flows run on the new lead as before)
tree = {"trigger": {"kind": "form_submitted", "form_id": None},
        "steps": [{"id": "t1", "type": "tag", "op": "add", "tag": "new lead"}]}
fid = ex("INSERT INTO flows (workspace_id, name, status, graph_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
         WS, "Welcome", "on", flows.pack(tree), db.now(), db.now())
vid = ex("INSERT INTO flow_versions (flow_id, version, graph_json, created_at) VALUES (?,?,?,?)", fid, 1, flows.pack(tree),
         db.now())
ex("UPDATE flows SET version_id=?, version=1 WHERE id=?", vid, fid)

TAGS = f"utm_source=facebook&utm_medium=paid&utm_campaign={CAMP}&utm_content={ADS[0]}&fbclid=IwAR0emberClick01"


def hidden(page_html):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page_html))


def send(c, path, page_html, email, extra=None, name="Jo"):
    data = hidden(page_html)
    data["_t"] = forms.stamp(SECRET, "emberq", now=S.time.time() - 5)
    data.update({"f_name": name, "f_email": email, "f_phone": "+1 512 555 0100"})
    data.update(extra or {})
    return c.post(path, data=data)


# ================================================================ 1. the page carries the ad's tags
print("the page and the hosted form carry the tags")
v = S.client()
r = v.get("/p/ember?" + TAGS)
h = hidden(html(r))
check(r.status_code == 200 and h.get("utm_source") == "facebook" and h.get("utm_campaign") == CAMP
      and h.get("utm_content") == ADS[0] and h.get("_fbclid") == "IwAR0emberClick01" and h.get("_page") == "ember",
      "the page's form carries utm_source, utm_campaign, utm_content and the fbclid")
check("_eid" not in h, "without conversion tracking there's no event id: the tags don't need it")
hf = hidden(html(S.client().get("/f/emberq?" + TAGS + "&utm_term=soy%20candle")))
check(hf.get("utm_content") == ADS[0] and hf.get("utm_term") == "soy candle" and hf.get("_fbclid") == "IwAR0emberClick01",
      "the hosted form carries them too, utm_term included")
bad = hidden(html(S.client().get('/p/ember?utm_content=%22%3E%3Cscript%3Ex&fbclid=a%22b')))
check(all("<" not in v_ and '"' not in v_ for v_ in bad.values()) and "_fbclid" not in bad,
      "odd characters are dropped and a malformed click id isn't carried")

# ================================================================ 2. a lead through the tagged page
print("a lead from an ad")
r = send(v, "/f/emberq", html(v.get("/p/ember?" + TAGS)), "ava@example.com", name="Ava")
check(r.status_code == 303 and "sent=emberq" in r.headers["Location"], "the lead goes back to the page's thank-you")
c1 = q("SELECT * FROM contacts WHERE workspace_id=? AND email=?", WS, "ava@example.com")[0]
src = q("SELECT * FROM lead_sources WHERE contact_id=?", c1["id"])
check(len(src) == 1 and src[0]["platform"] == "meta" and src[0]["launch_id"] == LID and src[0]["ad_external_id"] == ADS[0]
      and src[0]["campaign_external_id"] == CAMP and src[0]["matched"] == "ad" and src[0]["click_type"] == "fbclid"
      and src[0]["click_id"] == "IwAR0emberClick01" and src[0]["utm_content"] == ADS[0] and src[0]["page_slug"] == "ember",
      "one source row: Meta, the launch, the ad, the campaign, the click id, the tags, the page")
ev = q("SELECT * FROM contact_events WHERE contact_id=? AND kind='ad'", c1["id"])
check(len(ev) == 1 and ev[0]["detail"].startswith("Meta ad Evening ritual") and "campaign Ember Candle" in ev[0]["detail"],
      "the contact's timeline says which ad: Meta ad Evening ritual, campaign Ember Candle")
runs = q("SELECT * FROM flow_runs WHERE contact_id=? AND flow_id=?", c1["id"], fid)
check(len(runs) == 1, "the form_submitted flow started on the new lead, as before")
sub = q("SELECT * FROM form_submissions WHERE contact_id=?", c1["id"])[0]
check(sub["utm_source"] == "facebook" and sub["utm_campaign"] == CAMP and src[0]["submission_id"] == sub["id"],
      "the submission keeps its tags as before and the source row points at it")

page = html(owner.get(f"/app/contacts/{c1['id']}"))
check("Where they came from" in page and f"/app/ad-campaigns/{LID}" in page and "Meta click id" in page
      and f"utm_content={ADS[0]}" in page, "the contact page: the ad, a link to its campaign, the click kind, the tags")
check("IwAR0emberClick01" not in page, "the click id itself isn't shown")
lst = html(owner.get("/app/contacts"))
check("Meta ad</span>" in lst, "the contacts list tags them Meta ad")
csv_ = owner.get(f"/app/forms/{FORM}/submissions?format=csv").get_data(as_text=True)
head, row1 = csv_.splitlines()[0], csv_.splitlines()[1]
check("utm_content" in head and "ad_platform" in head and "ad_id" in head and ADS[0] in row1 and "meta" in row1,
      "the CSV export has utm_content, the platform and the ad id")
check("Meta ad" in html(owner.get(f"/app/forms/{FORM}/submissions")), "the submissions page tags the row")

# matching: campaign only, platform only, a stranger's ad id, Google tags
print("matching")
for email, tags, want in (
        ("cam@example.com", f"utm_source=facebook&utm_campaign={CAMP2}", ("meta", LID2, None, "campaign")),
        ("plat@example.com", "fbclid=IwAR0onlyAClick", ("meta", None, None, "platform")),
        ("strange@example.com", "utm_source=facebook&utm_content=99999999999", ("meta", None, None, "platform")),
        ("goog@example.com", "gclid=Cj0KCQiAgoogle123&utm_source=google&utm_content=555", ("google", None, None, "platform")),
        ("news@example.com", "utm_source=newsletter&utm_medium=email", (None, None, None, "none"))):
    cv = S.client()
    send(cv, "/f/emberq", html(cv.get("/p/ember?" + tags)), email)
    s_ = q("SELECT s.* FROM lead_sources s JOIN contacts c ON c.id=s.contact_id WHERE c.email=?", email)[0]
    check((s_["platform"], s_["launch_id"], s_["ad_external_id"], s_["matched"]) == want, f"{tags[:48]}: {want[3]}")
cv = S.client()
send(cv, "/f/emberq", html(cv.get("/p/ember")), "plain@example.com")
check(not q("SELECT 1 FROM lead_sources s JOIN contacts c ON c.id=s.contact_id WHERE c.email=?", "plain@example.com"),
      "a visit without tags or a click id keeps no source row")
# someone else's workspace can't claim our ad
_, ou2 = None, ou
with db.connect() as conn:
    res = adleads.resolve(conn, ou["workspace_id"], {"utm_source": "facebook", "utm_content": ADS[0]}, {})
check(res["launch_id"] is None and res["matched"] == "platform", "another workspace's tags never match our launches")

# ================================================================ 3. the Ad campaigns pages: leads and cost per lead
print("leads per ad with cost per lead")
# Meta says: ad 1 spent $30.00 over the last 7 days, ad 2 $12.00; Meta counted 1 lead
for n in range(1, 4):
    S.insight(WS, CONN, LID, ADS[0], CAMP, n, spend_minor=1000, imp=2000, link=40, results=1 if n == 1 else 0, leads=1 if n == 1 else 0)
    S.insight(WS, CONN, LID, ADS[1], CAMP, n, spend_minor=400, imp=1000, link=10)
# our forms: Ava (today) + Ben and Cy on ad 1 yesterday, Di on ad 2 yesterday; Ava sends again: still 1 lead
for email, ad in (("ben@example.com", ADS[0]), ("cy@example.com", ADS[0]), ("di@example.com", ADS[1]),
                  ("ava@example.com", ADS[0])):
    cv = S.client()
    tags = f"utm_source=facebook&utm_medium=paid&utm_campaign={CAMP}&utm_content={ad}&fbclid=IwAR0x{email[:2]}"
    send(cv, "/f/emberq", html(cv.get("/p/ember?" + tags)), email)
yday = S.day(1) + "T18:00:00+00:00"           # noon in Chicago yesterday
ex("UPDATE lead_sources SET created_at=? WHERE launch_id=? AND contact_id IN (SELECT id FROM contacts WHERE email IN "
   "('ben@example.com','cy@example.com','di@example.com'))", yday, LID)
ex("UPDATE lead_sources SET created_at=? WHERE launch_id=? AND id=(SELECT MAX(id) FROM lead_sources WHERE launch_id=?)",
   yday, LID, LID)                             # Ava's second form: yesterday too
with db.connect() as conn:
    L = conn.execute("SELECT * FROM launches WHERE id=?", (LID,)).fetchone()
    lf7 = adleads.leads_for(conn, L, "7", S.TZ, {ADS[0]: 3000, ADS[1]: 1200}, 4200)
    lft = adleads.leads_for(conn, L, "today", S.TZ, {}, 0)
check(lf7["total"] == 4 and lf7["by_ad"] == {ADS[0]: 3, ADS[1]: 1} and lf7["cpl"] == 1050,
      "last 7 days: 4 people (Ava twice is 1), 3 on ad 1 and 1 on ad 2, $42.00 / 4 = $10.50 per lead")
check(lf7["ad_cpl"] == {ADS[0]: 1000, ADS[1]: 1200}, "per ad: $30.00 / 3 = $10.00 and $12.00 / 1")
check(lft["total"] == 1 and lft["by_ad"] == {ADS[0]: 1}, "today: only Ava's first form (the 7-day range is complete days)")
p = html(owner.get(f"/app/ad-campaigns/{LID}"))
t = text(p)
check("From your own forms" in t and "Leads on your forms 4" in t and "Cost per lead, your forms $10.50" in t
      and "Leads Meta counted 1" in t, "the campaign page: 4 leads on your forms, $10.50 per lead, Meta counted 1")
check("Form leads" in p and re.search(r"Ad 1 .*?</th>.*?</td>\s*<td>3</td><td>\$10</td>", p, re.S)
      and re.search(r"Ad 2 .*?</th>.*?</td>\s*<td>1</td><td>\$12</td>", p, re.S), "the ads table: 3 leads at $10.00, 1 at $12.00")
check("Latest leads from this campaign" in p and p.count('href="/app/contacts/') >= 4,
      "the latest leads list links each one to their contact")
lst = re.sub(r"\s+([,.])", r"\1", text(html(owner.get("/app/ad-campaigns"))))
check("Your forms: 4 leads, $10.50 per lead. Meta counted 1." in lst, "the campaigns list: 4 leads, $10.50 per lead")
lt = text(html(owner.get("/app/ad-campaigns?range=today")))
check("2 leads from Meta ads Campaignify didn't launch" in lt and "1 lead from Google ads Campaignify didn't launch" in lt,
      "today: leads from ads we didn't launch are counted apart, per platform")
p14 = text(html(owner.get(f"/app/ad-campaigns/{LID}?range=today")))
check("Leads on your forms 1" in p14 and "Cost per lead, your forms None yet" in p14,
      "today: 1 lead and no cost yet (Meta has no spend for today)")
check(owner.get(f"/app/ad-campaigns/{LID}").status_code == 200 and other.get(f"/app/ad-campaigns/{LID}").status_code == 404,
      "another workspace can't open the campaign")

# ================================================================ 4. measurement off, the webhook, deleting a contact
print("measurement off, webhook, delete")
ex("UPDATE pages SET measurement='off' WHERE slug='ember'")
cv = S.client()
pg = html(cv.get("/p/ember?" + TAGS))
check("_fbclid" not in hidden(pg) and hidden(pg).get("utm_content") == ADS[0],
      "measurement off: the tags are carried, the click id isn't")
send(cv, "/f/emberq", pg, "off@example.com", extra={"_fbclid": "IwAR0forgedClick"})
s_ = q("SELECT s.* FROM lead_sources s JOIN contacts c ON c.id=s.contact_id WHERE c.email=?", "off@example.com")[0]
check(s_["click_id"] is None and s_["ad_external_id"] == ADS[0], "and a posted click id isn't kept: the ad still matches")
ex("UPDATE pages SET measurement='us' WHERE slug='ember'")

sent = []
forms.send_async = lambda url, secret, payload, done, sync=False: sent.append(payload)
ex("UPDATE forms SET webhook_url='https://hooks.example.com/x' WHERE id=?", FORM)
cv = S.client()
send(cv, "/f/emberq", html(cv.get("/p/ember?" + TAGS)), "hook@example.com")
check(sent and sent[-1]["ad"] == {"platform": "meta", "campaign_id": CAMP, "ad_id": ADS[0]}
      and sent[-1]["utm"]["utm_content"] == ADS[0], "the webhook carries the ad and utm_content")
ex("UPDATE forms SET webhook_url=NULL WHERE id=?", FORM)

cid = q("SELECT id FROM contacts WHERE email='hook@example.com'")[0]["id"]
owner.post(f"/app/contacts/{cid}", data={"csrf": S.csrf(owner), "action": "delete"})
check(not q("SELECT 1 FROM lead_sources WHERE contact_id=?", cid), "deleting the contact deletes its source rows")

# ================================================================ 5. behind AD_LAUNCH
os.environ["AD_LAUNCH"] = "off"
check(owner.get("/app/ad-campaigns").status_code == 404, "AD_LAUNCH off: the Ad campaigns pages stay hidden")
cv = S.client()
send(cv, "/f/emberq", html(cv.get("/p/ember?" + TAGS)), "flagoff@example.com")
check(q("SELECT s.launch_id FROM lead_sources s JOIN contacts c ON c.id=s.contact_id WHERE c.email=?",
        "flagoff@example.com")[0]["launch_id"] == LID, "the source is still kept, so the numbers are there when it's on")
os.environ["AD_LAUNCH"] = "on"

print(f"\n{S.passed[0]} checks passed")

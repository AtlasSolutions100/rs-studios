"""The weekly report email per workspace (roadmap M4.4, weekly_report.py).

    python tests/test_weekly_report.py      (tests/test_app.py runs it too)

  * when: Monday from 8:00 in the workspace's time zone, still Tuesday and Wednesday if it waited; never twice
  * only when there's something to report: a week with no spend and no form lead sends nothing
  * the fixture render: spend against the week before, results as Meta counts them, leads on your forms with cost
    per lead, the best ad, what to change (a worn ad to refresh, a campaign that spent with nothing), plain words,
    no dashes, links back to the app, the unsubscribe link and List-Unsubscribe headers
  * the send: nurture class (kind weekly_report), held by EMAIL_NURTURE_DAILY_MAX and tried again later the same week,
    the owner only, a failing mailer never mails twice
  * unsubscribe: one-click POST without login, the page's button, a bad token is a 404, the Ad campaigns page turns
    it back on; AD_LAUNCH off or WEEKLY_REPORT=0 sends nothing
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m4seed as S  # noqa: E402  (sets the environment first)

os.environ.update(RESEND_API_KEY="re_test_weekly", RS_MAIL_DOMAIN="mail.campaignify.test", EMAIL_NURTURE_DAILY_MAX="60")
import db  # noqa: E402
import forms  # noqa: E402
import mailer  # noqa: E402
import weekly_report as W  # noqa: E402
from m4seed import check, ex, html, q, text  # noqa: E402

SENT = []


def fake_attempt(body, key, shared):
    SENT.append({"body": body, "key": key})
    return "sent", "", None, f"msg_{len(SENT)}"


mailer._attempt = fake_attempt
SECRET = S.appmod.app.secret_key
CHI = ZoneInfo("America/Chicago")
# a fixed Monday, 9:00 in Chicago: the report covers the 7 days before it
now = datetime.now(CHI)
MONDAY = (now - timedelta(days=now.weekday() - 7)).replace(hour=9, minute=0, second=0, microsecond=0)
S.T[0] = MONDAY.timestamp()

owner, u = S.signup("weekly-owner@example.com")
WS = u["workspace_id"]
ex("UPDATE workspaces SET name='Ember Candle Co' WHERE id=?", WS)
member, mu = S.signup("weekly-member@example.com")
ex("UPDATE users SET workspace_id=?, role='member' WHERE id=?", WS, mu["id"])
CONN = S.connect(WS, u["id"])
LID, ADS, CAMP = S.launched(WS, u, S.make_plan(WS, u["id"]), "Ember Candle")
LID2, ADS2, CAMP2 = S.launched(WS, u, S.make_plan(WS, u["id"], "Gift set"), "Gift set")

# ================================================================ 1. when
print("when")
mon = MONDAY.replace(hour=7, minute=59)
check(W.week_of(mon) is None and W.week_of(mon.replace(hour=8, minute=0)) == MONDAY.date(),
      "Monday before 8:00 local: not yet; from 8:00: this week's report")
check(W.week_of(mon + timedelta(days=2)) == MONDAY.date() and W.week_of(mon + timedelta(days=3)) is None,
      "still due on Wednesday (if the email budget made it wait), not from Thursday")

# ================================================================ 2. nothing to report
print("nothing to report")
check(W.run_once(SECRET, now=S.T[0]) == 0 and not SENT, "a week with no spend and no leads: no email")
check(q("SELECT status FROM weekly_reports WHERE workspace_id=?", WS)[0]["status"] == "nothing",
      "noted as nothing, so later passes don't look again")
ex("DELETE FROM weekly_reports")

# ================================================================ 3. a week with numbers
print("the report")
for n in range(1, 15):
    this = n <= 7
    S.insight(WS, CONN, LID, ADS[0], CAMP, n, spend_minor=2000, imp=2000, link=44, results=1 if n % 2 else 0,
              leads=1 if n % 2 else 0)
    S.insight(WS, CONN, LID, ADS[1], CAMP, n, spend_minor=2000, imp=2000, link=24 if this else 40)
    S.insight(WS, CONN, LID2, ADS2[0], CAMP2, n, spend_minor=500 if this else 0, imp=900, link=5)
FIELDS = [{"key": "email", "label": "Email", "type": "email", "required": True, "options": [], "show_if": None}]
FORM = ex("INSERT INTO forms (workspace_id, code, name, fields_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
          WS, "weekq", "Quote", json.dumps(FIELDS), db.now(), db.now())
ex("INSERT INTO pages (workspace_id, slug, title, description, status, blocks_json, created_at, updated_at) VALUES "
   "(?,?,?,?,?,?,?,?)", WS, "ember-w", "Ember", "", "published", json.dumps([{"type": "form", "heading": "Quote",
                                                                              "form": FORM}]), db.now(), db.now())
for i in range(4):
    v = S.client()
    pg = html(v.get(f"/p/ember-w?utm_source=facebook&utm_medium=paid&utm_campaign={CAMP}&utm_content={ADS[0]}"))
    data = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', pg))
    data.update(_t=forms.stamp(SECRET, "weekq", now=__import__("time").time() - 5), f_email=f"lead{i}@example.com")
    v.post("/f/weekq", data=data)
ex("UPDATE lead_sources SET created_at=? WHERE workspace_id=?", S.day(2) + "T18:00:00+00:00", WS)

with db.connect() as conn:
    rep = W.build(conn, S.ws(WS), MONDAY.date())
check(rep is not None and rep["since"].isoformat() == S.day(7) and rep["until"].isoformat() == S.day(1),
      "the report covers the 7 days before Monday, in the ad account's dates")
subject, txt, htm = W.render(rep, "https://campaignify.test", "https://campaignify.test/u/weekly/TOKEN")
check(subject == "Your week on Meta: $315 spent, 4 leads on your forms",
      "subject: what was spent and the form leads ($140 + $140 + $35)")
check("Spent: $315, 12% more than the week before." in txt, "spend against the week before ($280)")
check("Ember Candle: $280 spent, 4 leads as Meta counts them at $70 each, 4 leads on your forms at $70 each, 1.70% click rate."
      in txt, "per campaign: spend, Meta's results and cost, form leads and cost per lead, click rate")
check("Best ad: Ad 1 Evening ritual in Ember Candle, with 4 leads at $35 each." in txt, "the best ad, by cost per result")
check("Refresh Ad 2 Lasts the month in Ember Candle. Its click rate fell 40% this week (2.00% to 1.20%)" in txt
      and f"/app/create?refresh={LID}:{ADS[1]}" in txt, "what to change: refresh the worn ad, with a link")
check("Gift set spent $35 with no leads and no form leads." in txt and f"/app/ad-campaigns/{LID2}" in txt,
      "what to change: a campaign that spent with nothing to show")
check("Stop these weekly emails: https://campaignify.test/u/weekly/TOKEN" in txt and 'href="https://campaignify.test/u/weekly/TOKEN"'
      in htm and "See every number" in htm, "the unsubscribe link and the way back to the numbers")
check(W._n(1, "Leads") == "1 lead" and W._n(2, "Leads") == "2 leads" and W._n(1, "People reached") == "1 person reached",
      "counts read right: 1 lead, 2 leads")
check("—" not in txt + htm and "–" not in txt + htm and not re.search(r"(?i)guarantee|you will get|double", txt),
      "plain words: no dashes, no promises")
os.makedirs(os.path.join(S.TMP, "out"), exist_ok=True)
open(os.environ.get("WEEKLY_FIXTURE_OUT") or os.path.join(S.TMP, "out", "weekly.html"), "w").write(htm)

n = W.run_once(SECRET, now=S.T[0])
check(n == 1 and len(SENT) == 1 and SENT[0]["body"]["to"] == ["weekly-owner@example.com"], "one email, to the owner only")
b = SENT[0]["body"]
check(b["subject"] == subject and {"name": "kind", "value": "weekly_report"} in b["tags"]
      and mailer.kind_class("weekly_report") == "nurture", "nurture class: kind weekly_report")
unsub = re.search(r"<(https://[^>]+/u/weekly/[^>]+)>", b["headers"]["List-Unsubscribe"]).group(1)
check(b["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and unsub in b["text"],
      "List-Unsubscribe with one-click POST, the same link in the email")
check(W.run_once(SECRET, now=S.T[0]) == 0 and W.run_once(SECRET, now=S.T[0] + 86400) == 0 and len(SENT) == 1,
      "never twice in a week (a pass later the same day, the next day)")

# ================================================================ 4. the nurture budget
print("nurture budget")
ex("DELETE FROM weekly_reports")
os.environ["EMAIL_NURTURE_DAILY_MAX"] = "1"
check(W.run_once(SECRET, now=S.T[0]) == 0 and len(SENT) == 1 and not q("SELECT 1 FROM weekly_reports"),
      "nurture cap reached today: nothing sent, nothing claimed")
os.environ["EMAIL_NURTURE_DAILY_MAX"] = "60"
check(W.run_once(SECRET, now=S.T[0] + 86400) == 1 and len(SENT) == 2, "a pass the next day sends it")


def failing(body, key, shared):
    return "failed", "bad address", None, None


ex("DELETE FROM weekly_reports")
mailer._attempt = failing
W.run_once(SECRET, now=S.T[0])
mailer._attempt = fake_attempt
check(q("SELECT status FROM weekly_reports")[0]["status"] == "failed" and W.run_once(SECRET, now=S.T[0]) == 0,
      "a failing send is recorded and never retried into a second email")

# ================================================================ 5. unsubscribe and switches
print("unsubscribe")
c = S.client()
r = c.get(unsub.replace("https://campaignify.test", ""))
check(r.status_code == 200 and "Stop the weekly report?" in html(r), "the link opens a page with one button, no login")
r = c.post(unsub.replace("https://campaignify.test", ""))
check(r.status_code == 200 and r.get_data(as_text=True) == "Unsubscribed."
      and q("SELECT weekly_report_off FROM users WHERE id=?", u["id"])[0]["weekly_report_off"],
      "one-click POST (no form, no CSRF, no login): unsubscribed")
check(c.get("/u/weekly/not-a-token").status_code == 404, "a bad token is a 404")
ex("DELETE FROM weekly_reports")
check(W.run_once(SECRET, now=S.T[0]) == 0, "unsubscribed: no report")
page = html(owner.get("/app/ad-campaigns"))
check("Weekly report email" in page and "Turn on" in page, "the Ad campaigns page shows it off, with Turn on")
owner.post("/app/ad-campaigns/weekly-report", data={"csrf": S.csrf(owner), "weekly": "on"})
check(q("SELECT weekly_report_off FROM users WHERE id=?", u["id"])[0]["weekly_report_off"] is None,
      "Turn on puts it back")
check("Every Monday, to weekly-owner@example.com" in html(owner.get("/app/ad-campaigns")), "and the page says so")
os.environ["AD_LAUNCH"] = "off"
check(W.run_once(SECRET, now=S.T[0]) == 0, "AD_LAUNCH off: nothing sent")
os.environ["AD_LAUNCH"] = "on"
os.environ["WEEKLY_REPORT"] = "0"
check(W.run_once(SECRET, now=S.T[0]) == 0, "WEEKLY_REPORT=0: nothing sent")
os.environ.pop("WEEKLY_REPORT")
check(W.run_once(SECRET, now=S.T[0]) == 1, "back on: it sends")

print(f"\n{S.passed[0]} checks passed")

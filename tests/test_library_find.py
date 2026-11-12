"""The Library's search and date filter (app.library, shown in the new look), and the desk's nudge for ad accounts
(desk.nudge), on a throwaway database:

  * search: by words in the ad (its brief, its headline, its label), with the other filters kept; wildcards typed by
    a member are plain characters; nothing found says so with a way back; another workspace's ads never show
  * date: the last 24 hours, 7 days or 30 days; an unknown value is ignored
  * the first look's Library has no search box and no date row, and works as before
  * the nudge: only once an ad is finished, never during the first steps, to the setup hub when launching is off;
    dismissed for good, per member

    python tests/test_library_find.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_libfind_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0", UI_V2="1")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "PLANS_ONLY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY", "AD_LAUNCH", "AD_LAUNCH_WORKSPACES"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import desk  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (400, 400), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.14.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def member(email):
    import onboarding
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.execute("UPDATE users SET email_verified_at=?, name='Kay Lee' WHERE email=?", (db.now(), email))
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (user(email)["workspace_id"],))
        conn.commit()
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": "Ember Candle", "photos": (png(), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    return c


def job(email, tool, brief, hook, status="done", days_ago=0.0):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        u = user(email)
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                           "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (u["workspace_id"], u["id"], tool, brief,
                                                                     json.dumps({"hook": hook, "label": hook}), status, 12, 0.1, at, at)).lastrowid
        conn.commit()
    return jid


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def shown(body):
    return [int(x) for x in re.findall(r'<article class="litem[^"]*" data-job="(\d+)"', body)]


a = member("a@example.com")
b = member("b@example.com")

print("== the desk's nudge")
body = page(a, "/app/desk")
check("data-nudge" not in body, "nothing made yet: no nudge")
J1 = job("a@example.com", "ad_image", "A candle on a wooden table, warm evening light", "Light it when the day ends")
body = page(a, "/app/desk")
check("data-nudge" in body and "Your ads are ready. Set up your ad accounts and export each ad in the platform's sizes." in body
      and 'href="/app/adhub"' in body.replace("/app/ad-accounts", "/app/adhub") or "Set up ad accounts" in body,
      "an ad is finished: the nudge points at the setup hub (launching is off here)")
check(body.index("data-guide=") < body.index("data-nudge") < body.index("data-ask-form"), "it sits between the agent and the box")
check("data-nudge" not in page(b, "/app/desk"), "another workspace with nothing made sees none")
with appmod.app.test_request_context("/app/desk"), db.connect() as conn:
    from flask import g
    w = conn.execute("SELECT * FROM workspaces WHERE id=?", (user("a@example.com")["workspace_id"],)).fetchone()
    g.ws, g.user = w, user("a@example.com")
    check(desk.nudge(conn, w, user("a@example.com"), {"state": "first"}) is None
          and desk.nudge(conn, w, user("a@example.com"), {"state": "brief"}) is None, "never during the first steps")
    conn.execute("UPDATE workspaces SET plan_override='free' WHERE id=?", (w["id"],))
    conn.commit()
    w2 = conn.execute("SELECT * FROM workspaces WHERE id=?", (w["id"],)).fetchone()
    import plans
    check(plans.has(w2, "adhub") or desk.nudge(conn, w2, user("a@example.com"), {"state": "next"}) is None,
          "and not to a page the plan does not include")
    conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (w["id"],))
    conn.commit()
r = a.post("/app/desk/nudge/off", data={"csrf": csrf(a)})
check(r.status_code == 302 and "data-nudge" not in page(a, "/app/desk"), "dismissed: it is gone")
job("a@example.com", "ad_image", "The candle in a gift box with ribbon", "The easy gift")
check("data-nudge" not in page(a, "/app/desk"), "and stays gone when more ads finish")
check(a.post("/app/desk/nudge/off", data={}).status_code in (400, 403), "dismissing needs the form's token")

print("== search")
J2 = shown(page(a, "/app/library"))[0]
J3 = job("a@example.com", "ugc_video", "Ember Candle. A soy wax candle with amber and cedar.", "Presenter video", days_ago=3)
J4 = job("a@example.com", "ad_image", "100% soy wax, 40_hours", "Burns 100% clean", days_ago=20)
J5 = job("a@example.com", "ad_image", "An old one", "Last season", status="failed", days_ago=45)
JB = job("b@example.com", "ad_image", "A gift for someone else", "The easy gift")
body = page(a, "/app/library")
check('id="lq" type="search" name="q"' in body and set(shown(body)) == {J1, J2, J3, J4, J5}, "the Library has a search box and shows everything")
check(shown(page(a, "/app/library?q=gift")) == [J2], "a word in the headline finds the ad")
check(shown(page(a, "/app/library?q=wooden+table")) == [J1], "words in the brief find it")
check(shown(page(a, "/app/library?q=GIFT")) == [J2], "upper or lower case")
check(set(shown(page(a, "/app/library?q=candle"))) == {J1, J2, J3}, "one word can find several")
check(shown(page(a, "/app/library?q=100%25")) == [J4] and shown(page(a, "/app/library?q=40_hours")) == [J4]
      and shown(page(a, "/app/library?q=%25")) == [J4] and shown(page(a, "/app/library?q=_")) == [J4],
      "a percent sign or an underscore typed by a member is a plain character")
body = page(a, "/app/library?q=zzzz")
check(not shown(body) and 'Nothing matches "zzzz".' in body and "Clear the search" in body, "nothing found says so, with a way back")
check(JB not in shown(page(a, "/app/library?q=gift")) and shown(page(b, "/app/library?q=gift")) == [JB], "another workspace's ads never show")
body = page(a, "/app/library?q=candle&type=image")
check(set(shown(body)) == {J1, J2} and 'name="type" value="image"' in body, "the other filters are kept with a search")
check('href="/app/library?q=candle&status=done"' in body.replace("&amp;", "&") or "q=candle" in re.search(r'<nav class="fchips".*?</nav>', body, re.S).group(0),
      "and the search is kept when a filter is picked")
check(len(re.search(r'name="q" value="([^"]*)"', page(a, "/app/library?q=" + "x" * 300)).group(1)) == 80, "a long search is cut to 80 characters")
x = page(a, '/app/library?q=%22%3E%3Cscript%3E')
check("<script>" not in a.get('/app/library?q=%22%3E%3Cscript%3E').get_data(as_text=True).split("</head>")[1].split('id="lq"')[1][:200],
      "what was typed is escaped on the page")

print("== date")
body = page(a, "/app/library")
check([x for x in re.findall(r'<a class="fchip[^"]*" href="[^"]*"[^>]*>([^<]+)</a>', re.search(r'<span class="small muted">Date</span>.*?</div>', body, re.S).group(0))]
      == ["Any time", "Last 24 hours", "Last 7 days", "Last 30 days"], "a Date row with three windows")
check(set(shown(page(a, "/app/library?when=day"))) == {J1, J2}, "the last 24 hours")
check(set(shown(page(a, "/app/library?when=week"))) == {J1, J2, J3}, "the last 7 days")
check(set(shown(page(a, "/app/library?when=month"))) == {J1, J2, J3, J4}, "the last 30 days")
check(set(shown(page(a, "/app/library?when=nonsense"))) == {J1, J2, J3, J4, J5}, "an unknown window is ignored")
check(shown(page(a, "/app/library?when=week&q=candle&type=talking")) == [J3], "date, search and type together")

print("== the first look")
os.environ.pop("UI_V2", None)
body = page(a, "/app/library")
check('name="q"' not in body and ">Date<" not in body and set(shown(body)) == {J1, J2, J3, J4, J5},
      "no search box and no date row; the Library is as it was")
check("data-nudge" not in page(a, "/app"), "and no nudge")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} library-find checks passed")

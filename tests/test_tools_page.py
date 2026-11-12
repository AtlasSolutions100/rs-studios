"""The Tools page and the done-for-you request (tools_page.py, web/v2/tools.html, web/v2/done_for_you.html), on a
throwaway database with email faked:

  * new look off: no Tools page, no done-for-you page, the first look's menu as it was
  * new look on: one catalog grouped by the job; a tile for everything the menu could open, with the plan that
    includes it where the workspace's plan does not; the menu keeps the daily places, Tools and Settings
  * nothing is lost: every item of the first look's menu that this workspace can open is in the new menu or on the
    Tools page
  * on a tool's own page the menu lists that tool's group, with the tool marked
  * done for you: the request is saved, then emailed to the address that handles them with the member's address as
    the reply-to; it needs words and an address; a few a day; saved even when email is off, and the page then gives
    the address to write to; DONE_FOR_YOU_EMAIL changes who gets it

    python tests/test_tools_page.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_tools_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0", MCP_SERVER="on")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "PLANS_ONLY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY", "DONE_FOR_YOU_EMAIL", "AD_LAUNCH", "CREATOR_POOLS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402
import tools_page  # noqa: E402

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


sent, mail_on = [], [True]
mailer.configured = lambda: mail_on[0]
_real_send = mailer.send


def fake_send(**kw):
    if not mail_on[0]:
        raise mailer.MailError("Email sending isn't connected.")
    sent.append(kw)
    return f"em_{len(sent)}"


mailer.send = fake_send
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.12.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def member(email, plan="starter"):
    import onboarding
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.execute("UPDATE users SET email_verified_at=?, name='Kay Lee' WHERE email=?", (db.now(), email))
        conn.execute("UPDATE workspaces SET plan_override=?, name='Hearth & Co' WHERE id=?", (plan, user(email)["workspace_id"]))
        conn.commit()
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": "Ember Candle", "photos": (png(), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    return c


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def side_of(body):
    return re.search(r'<aside class="side".*?</aside>', body, re.S).group(0)


def requests_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM service_requests WHERE workspace_id=? ORDER BY id",
                            (user(email)["workspace_id"],)).fetchall()


print("== new look off")
a = member("a@example.com")
r = a.get("/app/tools")
check(r.status_code == 302 and "/app/tools" not in r.headers["Location"], "no Tools page")
r = a.get("/app/done-for-you")
check(r.status_code == 302 and "done-for-you" not in r.headers["Location"], "no done-for-you page")
r = a.post("/app/done-for-you", data={"csrf": csrf(a), "wants": "Ten ads", "email": "a@example.com"})
check(r.status_code == 302 and not requests_of("a@example.com") and not sent, "and nothing is saved or sent")
old = side_of(page(a, "/app/library"))
check("<span>Tools</span>" not in old and "<summary>Plan" in old and "<summary>Grow" in old and "<span>Flows</span>" in old,
      "the first look's menu is as it was")
old_links = set(re.findall(r'<a class="nav[^"]*" href="([^"]+)"', old))

print("== new look on")
os.environ["UI_V2"] = "1"
body = page(a, "/app/tools")
check("<h1>Tools</h1>" in body and "data-tools-q" in body, "the Tools page opens with a search box")
groups = re.findall(r'<h2 id="tp-g\d+">([^<]+)</h2>', body)
check(groups == ["Make ads", "Plan", "Capture leads", "Follow up", "Launch"], "the tools are grouped by the job")
tiles = dict(re.findall(r'<a href="([^"]+)">\s*<span class="tp-ic"', body) and
             [(m.group(1), m.group(2)) for m in re.finditer(r'<a href="([^"]+)">\s*<span class="tp-ic".*?<b>([^<]+)</b>', body, re.S)])
check({"Studio", "Workflow", "Advanced", "Copy", "Products", "Planner", "Competitors", "Pages", "Forms", "Links", "Contacts",
       "Flows", "Sending", "Ad accounts"} <= set(tiles.values()), "every tool has a tile")
check(body.count("data-tool=") == len(tiles) == int(re.search(r'placeholder="Search (\d+) tools"', body).group(1)),
      "and the search box says how many there are")
check('data-tool="flows emails and texts that go out by themselves. email sms automation sequence follow up"' in body,
      "a tile is found by its name, its line, its group or a related word")
check(re.search(r"<b>Forms</b>.*?</span>\s*<em class=\"lock\" title=\"Included in Growth\">Growth</em>", body, re.S) is not None
      and re.search(r"<b>Studio</b>.*?</span>\s*</a>", body, re.S) is not None,
      "a tool the plan does not include carries the plan that does; the others carry no mark")
check("We make the ads for you" in body and 'href="/app/done-for-you"' in body, "done for you leads the page")
check("Make ads from Claude or ChatGPT" in body and 'href="/mcp"' in body, "with how to use it from an assistant, while that is on")
os.environ["MCP_SERVER"] = "off"
check("Make ads from Claude or ChatGPT" not in page(a, "/app/tools"), "and not while it is off")
os.environ["MCP_SERVER"] = "on"

side = side_of(body)
check(re.search(r'<a class="nav v2-tools on" href="/app/tools" aria-current=page>', side) is not None, "Tools is in the menu, lit on its page")
check("<summary>Settings" in side and all(f"<summary>{x}" not in side for x in ("Plan", "Grow", "Launch", "Tools", "More on paid plans")),
      "the menu keeps Settings and drops the other groups")
check("v2-toolgrp" not in side, "and lists no tool group on the Tools page itself")

print("== nothing is lost")
new_links = set(re.findall(r'<a class="nav[^"]*" href="([^"]+)"', side)) | set(tiles) \
    | set(re.findall(r'href="([^"]+)"', re.search(r'<details class="navgrp" data-navgrp="Settings".*?</details>', side, re.S).group(0)))
missing = sorted(x for x in old_links if x not in new_links and x not in ("/app", "/app/create"))
check(not missing, f"everything the first look's menu opens is in the new menu or on the Tools page ({missing or 'all there'})")
check("/app/create" in tiles and "/app/studio" in tiles, "Create is Advanced there, and Home's composer is Studio")
for href in sorted(tiles):
    r = a.get(href)
    check(r.status_code in (200, 402), f"{tiles[href]} opens ({href}: {r.status_code})")

print("== in a tool, the menu lists its group")
flows = side_of(page(a, "/app/flows"))
grp = re.search(r'<div class="v2-toolgrp".*?</div>', flows, re.S).group(0)
check('aria-label="Follow up"' in grp and re.search(r'<a class="nav on" href="/app/flows" aria-current=page>', grp) is not None,
      "on Flows: the Follow up group, with Flows marked")
check("<span>Sending</span>" in grp and "<span>Community</span>" in grp and "<span>Studio</span>" not in grp, "its neighbours, and no other group")
check(re.search(r'<a class="nav v2-tools " href="/app/tools" >', flows) is not None, "Tools itself is one click away")
studio = side_of(page(a, "/app/studio"))
check('aria-label="Make ads"' in studio and re.search(r'<a class="nav on" href="/app/studio" aria-current=page>', studio) is not None,
      "on Studio: the Make ads group")
prod = side_of(page(a, "/app/products/new"))
check('aria-label="Make ads"' in prod and re.search(r'<a class="nav on" href="/app/products" aria-current=page>', prod) is not None,
      "a page inside a tool (a new product) keeps its tool marked")
check("v2-toolgrp" not in side_of(page(a, "/app/library")) and "v2-toolgrp" not in side_of(page(a, "/app/billing")),
      "Library and Billing are not tools: no group")

print("== done for you")
body = page(a, "/app/done-for-you")
check("We make the ads for you" in body and 'name="wants"' in body and 'value="Ember Candle"' in body and 'value="a@example.com"' in body,
      "the form starts with the product and the account's address")
check("travis@" not in body, "the address that handles it is not printed while email works")
r = a.post("/app/done-for-you", data={"csrf": csrf(a), "sells": "Candles", "wants": "", "email": "a@example.com"})
check(r.status_code == 200 and "Say what you want made" in r.get_data(as_text=True) and not requests_of("a@example.com"),
      "it needs what you want made")
r = a.post("/app/done-for-you", data={"csrf": csrf(a), "sells": "Candles", "wants": "Ten video ads", "email": "nope"})
check(r.status_code == 200 and "email address we can reply to" in r.get_data(as_text=True) and not requests_of("a@example.com")
      and 'Ten video ads' in r.get_data(as_text=True), "and an address to reply to; what was typed is kept")
r = a.post("/app/done-for-you", data={"csrf": csrf(a), "sells": "Candles", "wants": "Ten video ads a month for <b>our</b> sale",
                                       "email": "kay@hearth.example", "phone": "555 0100"})
rows = requests_of("a@example.com")
check(r.status_code == 302 and len(rows) == 1 and rows[0]["wants"].startswith("Ten video ads") and rows[0]["sent_at"],
      "a request is saved and marked sent")
m = sent[-1]
check(len(sent) == 1 and m["to"] == "travis@rectorandstull.com" and m["reply_to"] == "kay@hearth.example",
      "it is emailed to the person who handles them, with the member's address as the reply-to")
check("Hearth & Co" in m["subject"] and "Ten video ads a month" in m["text"] and "555 0100" in m["text"] and "Kay Lee" in m["text"],
      "with the workspace, the words, the phone and the name")
check("<b>our</b>" not in m["html"] and "&lt;b&gt;our&lt;/b&gt;" in m["html"], "what the member typed is escaped in the email")
done = page(a, r.headers["Location"])
check("Sent. Travis will reply by email." in done and "kay@hearth.example" in done, "the page says it went and where the reply goes")
for i in range(2):
    a.post("/app/done-for-you", data={"csrf": csrf(a), "wants": f"More ads {i}", "email": "a@example.com"})
r = a.post("/app/done-for-you", data={"csrf": csrf(a), "wants": "And more", "email": "a@example.com"})
check(len(requests_of("a@example.com")) == 3 and len(sent) == 3 and "already sent a few requests today" in r.get_data(as_text=True),
      "three a day: a fourth is not saved or sent, and says why")

b = member("b@example.com", "growth")
mail_on[0] = False
r = b.post("/app/done-for-you", data={"csrf": csrf(b), "wants": "A launch video", "email": "b@example.com"})
rows = requests_of("b@example.com")
check(r.status_code == 302 and len(rows) == 1 and rows[0]["sent_at"] is None and len(sent) == 3, "with email off the request is still saved")
done = page(b, r.headers["Location"])
check("Saved." in done and 'href="mailto:travis@rectorandstull.com"' in done, "and the page gives the address to write to")
mail_on[0] = True
os.environ["DONE_FOR_YOU_EMAIL"] = "studio@example.org"
b.post("/app/done-for-you", data={"csrf": csrf(b), "wants": "Another", "email": "b@example.com"})
check(sent[-1]["to"] == "studio@example.org", "DONE_FOR_YOU_EMAIL changes who gets it")
os.environ.pop("DONE_FOR_YOU_EMAIL", None)
check(b.get(f"/app/done-for-you?sent={requests_of('a@example.com')[0]['id']}").status_code == 200
      and "kay@hearth.example" not in page(b, f"/app/done-for-you?sent={requests_of('a@example.com')[0]['id']}"),
      "another workspace's request is never shown")
check(re.search(r"<b>Forms</b>.*?</span>\s*</a>", page(b, "/app/tools"), re.S) is not None, "on Growth, Forms carries no mark")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} tools-page checks passed")

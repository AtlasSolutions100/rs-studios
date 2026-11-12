"""The Tools page and the done-for-you request (tools_page.py, web/v2/tools.html, web/v2/done_for_you.html), on a
throwaway database with email faked:

  * new look off: no Tools page, no done-for-you page, the first look's menu as it was
  * new look on: one catalog grouped by the job; a tile for everything the menu could open, with the plan that
    includes it where the workspace's plan does not; the menu keeps the daily places, Tools and Settings
  * nothing is lost: every item of the first look's menu that this workspace can open is in the new menu or on the
    Tools page
  * on a tool's own page the menu lists that tool's group, with the tool marked
  * use it from Claude: an in-app page with the server address, the steps for each client and prompts to copy, only
    while the MCP server is on
  * done for you: one button and nothing to fill in; a press saves a request with what the workspace already holds
    and emails it to whoever handles them, with the member's address as the reply-to; no person is named; a few a
    day; saved even when email is off, and the page then gives the site's address; DONE_FOR_YOU_EMAIL says who gets it

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
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0", MCP_SERVER="on",
                  DONE_FOR_YOU_EMAIL="Tee <tee@studio.example>")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "PLANS_ONLY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY", "AD_LAUNCH", "CREATOR_POOLS"):
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
check("Make ads from Claude" in body and 'href="/app/tools/assistant"' in body and "ChatGPT" not in body,
      "with how to use it from Claude, while that is on (no client is named that the docs do not cover)")
os.environ["MCP_SERVER"] = "off"
check("Make ads from Claude" not in page(a, "/app/tools"), "and not while it is off")
r = a.get("/app/tools/assistant")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/tools"), "the assistant page is closed while it is off")
os.environ["MCP_SERVER"] = "on"

print("== use it from Claude")
asst = page(a, "/app/tools/assistant")
check("<h1>Make ads from Claude</h1>" in asst and 'id="as-url" type="text" readonly value="https://campaignify.test/mcp"' not in asst
      and re.search(r'id="as-url" type="text" readonly value="http://localhost/mcp"', asst) is not None,
      "the page gives this site's server address, ready to copy")
check('data-copy="as-url"' in asst and 'data-copy="as-cmd"' in asst
      and 'value="claude mcp add --transport http campaignify http://localhost/mcp"' in asst, "and the Claude Code command")
check([x for x in re.findall(r'data-cat-pick="([a-z]+)"', asst)] == ["claude", "team", "code", "other"],
      "steps for Claude, Claude Team or Enterprise, Claude Code and other apps")
check("Add custom connector" in asst and "Organization settings" in asst and "mcp-remote" in asst and 'href="/docs/mcp"' in asst,
      "the same steps the public page and the docs give")
check(asst.count('class="as-kind"') == len(appmod.CLAUDE_PROMPTS) and asst.count("data-copy=\"as-p") == len(appmod.CLAUDE_PROMPTS),
      "every prompt can be copied")
desk_links = re.findall(r'<a class="as-desk" href="([^"]+)"', asst)
check(len(desk_links) == 3 and all(x.startswith("/app/desk?ask=") and "Ember" in x for x in desk_links),
      "three of them can be done on the desk instead, for this product")
check(all(a.get(x.replace("&amp;", "&")).status_code == 200 for x in desk_links), "and those links open")
check('href="/app/settings/mcp"' in asst or "MCP access" in asst, "with the way to the keys and connected apps")
check("isn't made or endorsed by Anthropic" in asst, "and the note that Campaignify is independent of Anthropic")
check(re.search(r'<a class="nav v2-tools on" href="/app/tools"', side_of(asst)) is not None, "Tools stays lit in the menu")

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

print("== done for you: one button")
with db.connect() as conn:
    wid = user("a@example.com")["workspace_id"]
    conn.execute("UPDATE products SET description=?, price='$34', offer='Free shipping this week', claims=?, audience=?, url=? "
                 "WHERE workspace_id=?", ("A soy wax candle with <b>amber</b> and cedar.", "Burns for 40 hours\nHand poured in Texas",
                                          "People who light a candle most evenings", "https://hearth.example/p/ember", wid))
    conn.execute("UPDATE brands SET brand_name='Hearth & Co', tone='Calm and warm', website='https://hearth.example' WHERE workspace_id=?", (wid,))
    conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (wid, user("a@example.com")["id"], "ad_image", "x", "{}", "done", 12, 0.1, db.now(), db.now()))
    conn.commit()
body = page(a, "/app/done-for-you")
check("We make the ads for you" in body and "Contact our team of professionals</button>" in body, "the page is one button")
check(not re.search(r'<(input|textarea)[^>]*name="(wants|sells|email|phone)"', body), "with nothing to fill in")
check("a@example.com" in body and "what your workspace already holds" in body, "it says what is sent and where the reply goes")
check("Travis" not in body and "tee@studio.example" not in body, "no person is named and no address of ours is printed")
check("Contact our team of professionals" in page(a, "/app/tools"), "the Tools card says the same")
r = a.post("/app/done-for-you", data={"csrf": csrf(a)})
rows = requests_of("a@example.com")
check(r.status_code == 302 and len(rows) == 1 and rows[0]["sent_at"] and rows[0]["email"] == "a@example.com",
      "one press saves a request and marks it sent")
m = sent[-1]
check(len(sent) == 1 and m["to"] == "tee@studio.example" and m["reply_to"] == "a@example.com",
      "it is emailed to whoever handles them, with the member's address as the reply-to")
txt = m["text"]
check("Hearth & Co (#" in txt and "Kay Lee" in txt and "Plan: Starter" in txt and "Hearth & Co" in m["subject"],
      "the email says who is asking: the name, the workspace and its plan")
check("Ember Candle" in txt and "$34" in txt and "Free shipping this week" in txt and "https://hearth.example/p/ember" in txt
      and "Burns for 40 hours; Hand poured in Texas" in txt and "People who light a candle most evenings" in txt,
      "the product: its price, offer, page, approved claims and who buys it")
check("Voice: Calm and warm" in txt and "Website: https://hearth.example" in txt, "the brand: its voice and website")
check("Finished ads: 1 image ad\n" in txt + "\n", "and what they have made here")
check("Offer: \n" not in txt and "Also said" not in txt, "a field with nothing in it is left out")
check("<b>amber</b>" not in m["html"] and "&lt;b&gt;amber&lt;/b&gt;" in m["html"], "what the member wrote is escaped in the email")
check(rows[0]["details"] and "Ember Candle" in rows[0]["details"], "the same details are kept with the request")
done = page(a, r.headers["Location"])
check("Request sent. Our team will reply by email." in done and "a@example.com" in done and "Travis" not in done,
      "the page says it went and where the reply goes")
for i in range(2):
    a.post("/app/done-for-you", data={"csrf": csrf(a)})
r = a.post("/app/done-for-you", data={"csrf": csrf(a)})
check(len(requests_of("a@example.com")) == 3 and len(sent) == 3 and "already asked a few times today" in r.get_data(as_text=True),
      "three a day: a fourth is not saved or sent, and says why")
check("You last asked on" in page(a, "/app/done-for-you"), "and the page remembers the last request")

b = member("b@example.com", "growth")
mail_on[0] = False
r = b.post("/app/done-for-you", data={"csrf": csrf(b)})
rows = requests_of("b@example.com")
check(r.status_code == 302 and len(rows) == 1 and rows[0]["sent_at"] is None and len(sent) == 3, "with email off the request is still saved")
import public_site  # noqa: E402
done = page(b, r.headers["Location"])
check("Saved." in done and f'href="mailto:{public_site.CONTACT}"' in done and "tee@studio.example" not in done,
      "and the page gives the site's contact address to write to")
mail_on[0] = True
os.environ["DONE_FOR_YOU_EMAIL"] = "studio@example.org"
b.post("/app/done-for-you", data={"csrf": csrf(b)})
check(sent[-1]["to"] == "studio@example.org" and "Plan: Growth" in sent[-1]["text"], "DONE_FOR_YOU_EMAIL changes who gets it (a bare address works)")
os.environ.pop("DONE_FOR_YOU_EMAIL", None)
b.post("/app/done-for-you", data={"csrf": csrf(b)})
check(sent[-1]["to"] == public_site.CONTACT and tools_page.dfy_email() == public_site.CONTACT,
      "unset, requests go to the site's contact address (no owner address in the source)")
os.environ["DONE_FOR_YOU_EMAIL"] = "Tee <tee@studio.example>"
check(b.get(f"/app/done-for-you?sent={requests_of('a@example.com')[0]['id']}").status_code == 200
      and "a@example.com" not in page(b, f"/app/done-for-you?sent={requests_of('a@example.com')[0]['id']}"),
      "another workspace's request is never shown")
check(re.search(r"<b>Forms</b>.*?</span>\s*</a>", page(b, "/app/tools"), re.S) is not None, "on Growth, Forms carries no mark")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} tools-page checks passed")

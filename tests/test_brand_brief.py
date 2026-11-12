"""The brand brief, the new look's last onboarding step (onboarding.brief, web/v2/onboard_brief.html):

  * new look off: there is no brief; members see no change
  * the profile step leads to it and keeps its own answers; the brief follows a visit to Studio until it is finished
    or skipped, and never sends the card page in a circle
  * no product yet: paste a link, the first-run importer reads it (faked here); an unreadable page says why
  * the cards edit what every prompt reads (core.brand): what you sell, who buys it, what you can say, how you sound;
    the brand's colors are left alone; a blocked claim isn't saved
  * claims found on the page are only used once the member ticks them
  * a workspace already set up for another business: the brief says the website typed is not the brand on screen,
    and offers to read it as a new brand (the plan's brand limit applies; the first brand is left as it was) or to
    keep the brand shown; the same site, or a link read in this step, shows no such note
  * angles: your own are free; "Suggest five" shows its price, holds and settles credits like Create does, says so
    when the balance can't cover it, and isn't offered when AI writing is off

    python tests/test_brand_brief.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_brief_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

from flask import jsonify  # noqa: E402

import app as appmod  # noqa: E402
import core  # noqa: E402
import create_routes  # noqa: E402
import creator  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import onboarding  # noqa: E402
import products  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.9.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
    return c, u["workspace_id"], u["id"]


def user(uid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def ws_row(ws):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws,)).fetchone()


def post(c, **data):
    return c.post("/app/welcome/brand", data=dict(data, csrf=csrf(c)))


def text(r):
    return htmllib.unescape(r.get_data(as_text=True))


def page(c):
    return text(c.get("/app/welcome/brand"))


print("== new look off")
a, wa, ua = signup("a@example.com")
r = a.get("/app/welcome/brand")
check(r.status_code == 302 and r.headers["Location"].rstrip("/").endswith("/app"), "no brief without the new look")

print("== the steps lead to it")
os.environ["UI_V2"] = "1"
r = a.post("/app/welcome/profile", data={"csrf": csrf(a), "first_name": "Ana", "last_name": "Diaz", "company": "Ember Candle Co.",
                                         "website": "embercandle.example"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome/brand"), "the profile step goes on to the brief")
ans = onboarding.answers(user(ua))
check(ans.get("brief") == "due" and ans.get("company") == "Ember Candle Co." and "embercandle.example" in (ans.get("website") or ""),
      "the profile answers are kept alongside the brief flag")
r = a.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome/brand"), "Studio sends a new account to the brief while it is waiting")
r = a.get("/app/card")
check(not (r.status_code == 302 and "/app/welcome/brand" in r.headers.get("Location", "")), "the card page is never sent to the brief (no circle)")
b2, wb2, ub2 = signup("skip@example.com")
r = b2.post("/app/welcome/profile", data={"csrf": csrf(b2), "skip": "1"})
check(r.headers["Location"].rstrip("/").endswith("/app") and onboarding.answers(user(ub2)).get("brief") is None,
      "skipping the profile skips the brief too")

print("== no product yet")
html = page(a)
check("Start with your link" in html and 'name="do" value="import"' in html and "embercandle.example" in html,
      "the link step is shown with the website already typed in")
real_import = appmod.app.view_functions["start_import"]
appmod.app.view_functions["start_import"] = lambda: (jsonify({"error": "That store doesn't let apps read its pages.", "manual": True}), 422)
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "import", "url": "https://shop.example/p/1"}, follow_redirects=True)
check("That store doesn't let apps read its pages." in text(r) and ws_row(wa)["product_id"] is None,
      "an unreadable page says why and nothing is made")


def fake_import():
    from flask import g, request
    assert request.form.get("url") == "https://embercandle.example/candle"
    with db.connect() as conn:
        w = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
        bid = products.ensure_brand(conn, w["id"])
        pid = products.create_product(conn, w, bid, {"name": "Ember Candle", "description": "A soy wax candle with amber and cedar.",
                                                     "price": "$34", "url": "https://embercandle.example/candle"},
                                      pending_claims=["Hand poured in Texas", "Burns for 40 hours", "Soy wax, cotton wick"],
                                      import_json={"url": "https://embercandle.example/candle", "images": []})
        products.activate(conn, w, product_id=pid)
        conn.commit()
    return jsonify({"ok": True, "product_id": pid, "name": "Ember Candle"})


appmod.app.view_functions["start_import"] = fake_import
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "import", "url": "https://embercandle.example/candle"}, follow_redirects=True)
html = text(r)
pid = ws_row(wa)["product_id"]
check(pid and "Here is what I understood" in html and "Ember Candle, $34" in html and "Read from embercandle.example/candle" in html,
      "after the read, the brief shows what was understood and where it came from")
appmod.app.view_functions["start_import"] = real_import

print("== the cards edit what every prompt reads")
with db.connect() as conn:
    bid = products.get_product(conn, wa, pid)["brand_id"]
    conn.execute("UPDATE brands SET kit_primary='#112233' WHERE id=?", (bid,))
    conn.commit()
post(a, do="product", name="Ember Candle", description="A soy wax candle with amber and cedar.", price="$36", offer="Free shipping this week")
post(a, do="product", audience="People who light a candle most evenings")
post(a, do="brand", tone="Calm, warm, a little dry", about="Small-batch candles from Austin.")
with appmod.app.test_request_context():
    with db.connect() as conn:
        brief = products.brief_for(conn, ws_row(wa), pid)
check(brief["price"] == "$36" and brief["offer"] == "Free shipping this week" and brief["audience"] == "People who light a candle most evenings"
      and brief["tone"] == "Calm, warm, a little dry" and brief["about"] == "Small-batch candles from Austin.",
      "price, offer, audience, tone and about land in the brief the prompt builders read")
with db.connect() as conn:
    check(conn.execute("SELECT kit_primary FROM brands WHERE id=?", (bid,)).fetchone()[0] == "#112233", "saving the voice leaves the brand's colors alone")
    check(products.get_product(conn, wa, pid)["description"] == "A soy wax candle with amber and cedar.", "saving one card leaves the other fields alone")
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "product", "description": "Cures insomnia, guaranteed."}, follow_redirects=True)
with db.connect() as conn:
    check("Not saved" in text(r) and "insomnia" not in products.get_product(conn, wa, pid)["description"],
          "a claim the house rules block isn't saved, and the page says so")
html = page(a)
check("Offer right now: Free shipping this week" in html and "Calm, warm, a little dry" in html, "the cards show the saved words")

print("== claims found on the page")
check("Found on your page" in html and html.count('name="pick"') == 3 and "Nothing approved yet" in html, "suggested claims wait to be ticked; none is in use yet")
post(a, do="claims")
with db.connect() as conn:
    check(products.claims_list(products.get_product(conn, wa, pid)["claims"]) == [], "adding with nothing ticked adds nothing")
a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "claims", "pick": ["0", "2"]})
with db.connect() as conn:
    p = products.get_product(conn, wa, pid)
check(products.claims_list(p["claims"]) == ["Hand poured in Texas", "Soy wax, cotton wick"] and products.pending(p) == ["Burns for 40 hours"],
      "only the ticked ones become things the ads may say")

print("== angles")
post(a, do="angle", title="The easy gift", hook="For the person who has everything.")
post(a, do="angle", title="", hook="")
with db.connect() as conn:
    rows = conn.execute("SELECT title, source FROM angles WHERE product_id=?", (pid,)).fetchall()
check([(r["title"], r["source"]) for r in rows] == [("The easy gift", "user")], "your own angle is saved; an empty one isn't")
html = page(a)
check("Suggest five" not in html and "The easy gift" in html, "with AI writing off, suggesting isn't offered")
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "angles"}, follow_redirects=True)
check("AI writing isn't switched on yet" in text(r), "and asking anyway says so and charges nothing")

llm.anthropic_ready = lambda: True
creator.estimate_angles = lambda: 9
html = page(a)
check("Suggest five · up to 9 credits" in html and "never more than the price on the button" in html, "with AI writing on, the button shows the most it can cost")
core.reserve_ai = lambda quote, label, ref=None, conn=None: None
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "angles"}, follow_redirects=True)
check("That needs about 9 credits" in text(r), "a balance that can't cover it says so before the writer is called")
held, settled, asked = [], [], []
core.reserve_ai = lambda quote, label, ref=None, conn=None: held.append((quote, label)) or "hold-1"


def fake_angles(b, ad_type, n=5, avoid=(), usage=None):
    asked.append((b.get("product"), b.get("price"), b.get("tone"), list(avoid)))
    usage.calls = 1
    return [{"title": "The evening ritual", "hook": "Light it when the day ends.", "insight": "For people who light one most nights."},
            {"title": "Made by hand", "hook": "Poured in Texas, one batch at a time.", "insight": "The page says it."},
            {"title": "The long burn", "hook": "Forty hours of evenings."}]


creator.angles = fake_angles
create_routes._settle = lambda hold, usage: settled.append((hold, usage.calls)) or 6
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "angles"}, follow_redirects=True)
html = text(r)
with db.connect() as conn:
    rows = conn.execute("SELECT title, source FROM angles WHERE product_id=? ORDER BY id", (pid,)).fetchall()
check(held == [(9, "AI angle ideas")] and settled == [("hold-1", 1)], "the most it can cost is held, and what the writing used is charged")
check(asked and asked[0][:3] == ("Ember Candle", "$36", "Calm, warm, a little dry") and asked[0][3] == ["The easy gift"],
      "the writer gets the corrected brief and is told which angles exist already")
check(len(rows) == 4 and [r["source"] for r in rows][1:] == ["ai", "ai", "ai"] and "3 angles added for 6 credits." in html
      and "Light it when the day ends." in html, "the angles are saved, shown, and the page says what was charged")

print("== finishing")
r = post(a, do="done")
check(r.status_code == 302 and r.headers["Location"].rstrip("/").endswith("/app") and onboarding.answers(user(ua)).get("brief") == "done",
      "Open Studio finishes the brief")
r = a.get("/app")
check(not (r.status_code == 302 and "/app/welcome" in r.headers.get("Location", "")), "Studio no longer sends them back")
check("Here is what I understood" in page(a), "the brief can still be opened by hand")
print("== a workspace already set up for another business")
check("data-bb-other" not in page(a), "the site they typed and read themselves: no note")


def set_up_for_clipwell(email, plan):
    """An account whose workspace already has a brand and product, answering the questions with another website."""
    c, w, u = signup(email)
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, w))
        conn.commit()
        row = conn.execute("SELECT * FROM workspaces WHERE id=?", (w,)).fetchone()
        bid = products.ensure_brand(conn, w)
        conn.execute("UPDATE brands SET brand_name='Clipwell', website='https://clipwell.example' WHERE id=?", (bid,))
        pid = products.create_product(conn, row, bid, {"name": "Clipwell", "description": "Clips from long videos.",
                                                       "url": "https://clipwell.example"},
                                      import_json={"url": "https://clipwell.example", "images": []})
        products.activate(conn, row, product_id=pid)
        conn.commit()
    c.post("/app/welcome/profile", data={"csrf": csrf(c), "first_name": "Tee", "last_name": "Rowe", "company": "Roof Longevity",
                                         "website": "www.RoofLongevity.example"})
    return c, w, u, bid, pid


def brand_names(w):
    with db.connect() as conn:
        return [r["brand_name"] for r in conn.execute("SELECT brand_name FROM brands WHERE workspace_id=? ORDER BY id", (w,))]


t1, wt, ut, bt, pt = set_up_for_clipwell("tee@example.com", "growth")
html = page(t1)
check("You entered rooflongevity.example." in html and "already set up for Clipwell, so that is the brand shown here" in html,
      "the brief says the website typed is not the brand on screen")
check("Read rooflongevity.example as a new brand" in html and "Keep Clipwell" in html and "Clipwell" in html.split("bb-head")[1],
      "it offers both choices, and still shows the brand the workspace has")
post(t1, do="other", url="https://somewhere-else.example")
check(brand_names(wt) == ["Clipwell"] and ws_row(wt)["product_id"] == pt, "a different address in the form makes nothing")
seen = {}


def fake_roof_import():
    from flask import g, request
    seen["url"], seen["brand"] = request.form.get("url"), g.ws["brand_id"]
    with db.connect() as conn:
        w = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
        pid = products.create_product(conn, w, g.ws["brand_id"], {"name": "Roof check", "description": "A yearly roof inspection.",
                                                                   "url": "https://rooflongevity.example"},
                                      import_json={"url": "https://rooflongevity.example", "images": []})
        products.activate(conn, w, product_id=pid)
        conn.commit()
    return jsonify({"ok": True, "product_id": pid, "name": "Roof check"})


appmod.app.view_functions["start_import"] = fake_roof_import
r = t1.post("/app/welcome/brand", data={"csrf": csrf(t1), "do": "other", "url": "https://www.RoofLongevity.example"},
            follow_redirects=True)
html = text(r)
check(brand_names(wt) == ["Clipwell", "Roof Longevity"] and seen["brand"] != bt and "rooflongevity" in seen["url"].lower(),
      "read as a new brand: a second brand, named as they named their company, and the page is read into it")
check(ws_row(wt)["brand_id"] == seen["brand"] and "Roof check" in html and "Read from rooflongevity.example" in html
      and "data-bb-other" not in html, "the new brand is the one on screen, with no note left")
with db.connect() as conn:
    old = products.get_product(conn, wt, pt)
    check(old["name"] == "Clipwell" and old["brand_id"] == bt and conn.execute(
        "SELECT website FROM brands WHERE id=?", (bt,)).fetchone()[0] == "https://clipwell.example",
        "the first brand and its product are as they were")

k1, wk, uk, bk, pk = set_up_for_clipwell("kept@example.com", "growth")
post(k1, do="keep")
html = page(k1)
check("data-bb-other" not in html and brand_names(wk) == ["Clipwell"] and ws_row(wk)["product_id"] == pk
      and "Here is what I understood" in html, "keep: the note goes, nothing is made, the brand stays")
check(seen["brand"] != bk, "(and nothing was read)")

o1, wo, uo, bo, po = set_up_for_clipwell("one@example.com", "starter")
r = o1.post("/app/welcome/brand", data={"csrf": csrf(o1), "do": "other", "url": "https://rooflongevity.example"},
            follow_redirects=True)
html = text(r)
check(brand_names(wo) == ["Clipwell"] and ws_row(wo)["product_id"] == po and "Your plan includes 1 brand." in html
      and "choose to keep Clipwell" in html and "data-bb-other" in html,
      "a plan with one brand: said so, nothing made, both choices still there")

appmod.app.view_functions["start_import"] = lambda: (jsonify({"error": "That page couldn't be read.", "manual": True}), 422)
f1, wf, uf, bf, pf = set_up_for_clipwell("fail@example.com", "growth")
r = f1.post("/app/welcome/brand", data={"csrf": csrf(f1), "do": "other", "url": "https://rooflongevity.example"},
            follow_redirects=True)
html = text(r)
check("That page couldn't be read." in html and "Start with your link" in html and brand_names(wf) == ["Clipwell", "Roof Longevity"],
      "the page can't be read: the new brand waits for a link, and says why")
with db.connect() as conn:
    check(products.get_product(conn, wf, pf)["brand_id"] == bf, "and the first brand still has its product")
appmod.app.view_functions["start_import"] = real_import
check(onboarding._host("https://www.Roof-Longevity.example/roofs?x=1") == "roof-longevity.example"
      and onboarding._host("RoofLongevity.example") == "rooflongevity.example" and onboarding._host("") == "",
      "an address is compared by its site, without www or capitals")

os.environ.pop("UI_V2")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

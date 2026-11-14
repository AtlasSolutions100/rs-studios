"""Inspiration by category (inspiration.py) on the desk and the Tools page, and the brand step's "what you'll get"
cards, on a throwaway database:

  * the groups: campaigns, presenter videos, try-ons, apps and software, unboxing, product videos, local businesses, image ads, in that order (a local
    business's example sits in its own row, and one with a presenter still asks for a presenter); every card is one of our own
    files under static/showcase; a card either fills the desk's box with a sentence for this product or opens Studio
    with its preset picked, never both and never neither
  * the desk: a chip per kind, one row showing, six cards a row, a way to the rest; a sentence handed over in the
    address is in the box (cleaned and capped)
  * the Tools page: the Inspiration section with every card as a link that opens
  * the brand step before a link is read: four example cards, said to be a made-up brand

    python tests/test_inspiration.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_insp_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0", UI_V2="1")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "PLANS_ONLY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import desk  # noqa: E402
import inspiration  # noqa: E402
import presets  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.13.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def signup(email):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    return c


def member(email):
    import onboarding
    c = signup(email)
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.execute("UPDATE users SET email_verified_at=? WHERE email=?", (db.now(), email))
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (user(email)["workspace_id"],))
        conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (user(email)["workspace_id"], user(email)["id"], "ad_image", "x",
                                                               "{}", "done", 12, 0.1, db.now(), db.now()))
        conn.commit()
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": "Ember Candle", "photos": (png(), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    return c


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


print("== the groups")
a = member("a@example.com")
with appmod.app.test_request_context("/app/desk"), db.connect() as conn:
    import products
    w = conn.execute("SELECT * FROM workspaces WHERE id=?", (user("a@example.com")["workspace_id"],)).fetchone()
    p = products.get_product(conn, w["id"], w["product_id"])
    from flask import g
    g.ws, g.user = w, user("a@example.com")
    groups = inspiration.groups(p)
    bare = inspiration.groups(None)
# store clips (libstore.py) haven't arrived on a test machine, so the try-on row (store clips only) is left out here
check([x["key"] for x in groups] == ["campaigns", "presenter", "apps", "unboxing", "product", "local", "image"]
      and [k for k, _n in inspiration.CATS] == ["campaigns", "presenter", "tryon", "apps", "unboxing", "product", "local", "image"],
      "campaigns, presenter videos, (try-ons), apps and software, unboxing, product videos, local businesses, image ads")
apps = next(x for x in groups if x["key"] == "apps")
unbox = next(x for x in groups if x["key"] == "unboxing")
check(any(c["title"] == "Presenter video" for c in apps["items"]) and all("real screen" in c["ask"] for c in apps["items"] if c["ask"])
      and any(c["href"].endswith("preset=pov-unboxing") for c in unbox["items"]),
      "the app demo and the unboxing clip have rows of their own, and an app card asks for the format, not a plain presenter video")
local = next(x for x in groups if x["key"] == "local")
# most local clips are library-store clips (libstore.py), which haven't arrived on a test machine: count the manifest's
_raw_local = [it for it in json.load(open(os.path.join(ROOT, "static", "showcase", "manifest.json")))["items"]
              if it.get("group") == "local"]
check(local["name"] == "Local businesses" and len(local["items"]) >= 2 and len(_raw_local) >= 8
      and all(it["kind"] == "video" for it in _raw_local) and all(c["video"] and c["where"] == "desk" for c in local["items"]),
      "local businesses have a row of their own: roofing, a salon, a food truck and the like")
lawn = next(c for c in local["items"] if c["title"] == "Lawn care")
roof = next(c for c in local["items"] if c["title"] == "Smash burger")
check("presenter video" in lawn["ask"] and "the work being done, no one talking" in roof["ask"],
      "there, an example with a presenter asks for a presenter video, and one without asks for the work on screen")
check(not any(c["title"] in ("Lawn care", "Smash burger") for x in groups if x["key"] in ("presenter", "product") for c in x["items"]),
      "and it is listed once, not again under presenter or product videos")
check(len(groups[0]["items"]) == len(desk.EXAMPLES), "the campaigns are the desk's own examples")
cards = [c for x in groups for c in x["items"]]
check(all(bool(c["ask"]) != bool(c["href"]) for c in cards), "a card fills the box or opens Studio: one of the two")
check(all(c["image"].startswith("/static/showcase/") and (not c["video"] or c["video"].startswith("/static/showcase/"))
          for c in cards), "every picture and clip is one of our own files")
check(all(os.path.exists(os.path.join(ROOT, c["image"].lstrip("/"))) for c in cards), "and each file is there")
check(all("Ember Candle" in c["ask"] for c in cards if c["ask"]), "a sentence is written for this product")
studio = [c for c in cards if c["href"]]
check(studio and all(re.fullmatch(r"/app/studio\?preset=[a-z0-9-]+", c["href"]) and presets.get(c["href"].split("=")[1]) is not None
                     for c in studio), "a card made with a preset opens Studio with that preset")
check(all(len(x["items"]) <= inspiration.PER_CAT for x in groups) and all(c["title"] for c in cards), "each kind is capped, each card named")
check(all("your product" in c["ask"] for x in bare for c in x["items"] if c["ask"]), "with no product yet the sentences say your product")

print("== on the desk")
body = page(a, "/app/desk")
chips = re.findall(r'data-cat-pick="([a-z]+)" aria-pressed="(true|false)">([^<]+)<', body)
check(chips == [("campaigns", "true", "Campaigns"), ("presenter", "false", "Presenter videos"),
                ("apps", "false", "Apps and software"), ("unboxing", "false", "Unboxing"), ("product", "false", "Product videos"),
                ("local", "false", "Local businesses"), ("image", "false", "Image ads")], "a chip for each kind, the first one on")
rows = re.findall(r'<div class="dk-ex-row" data-cat="([a-z]+)" (hidden)?', body)
check(rows == [("campaigns", "")] + [(k, "hidden") for k in ("presenter", "apps", "unboxing", "product", "local", "image")],
      "one row shows at a time")
for key in ("campaigns", "presenter", "product", "image"):
    row = re.search(rf'<div class="dk-ex-row" data-cat="{key}".*?</div>\s*(?=<div class="dk-ex-row"|\s*<p class="dk-exs-more"|\s*</section>)', body, re.S).group(0)
    check(0 < row.count('class="dk-ex"') <= 6, f"{key}: at most six cards on the desk")
check('href="/app/tools#inspiration">More examples</a>' in body, "and a way to the rest")
check('<a class="dk-ex" href="/app/studio?preset=' in body and 'class="dk-ex" data-try="One presenter video for Ember Candle' in body,
      "preset cards are links to Studio; the others fill the box")
body = page(a, "/app/desk?ask=Four%20image%20ads%20for%20the%20%3Cb%3Esale%3C/b%3E")
check(re.search(r'data-ask>Four image ads for the <b>sale</b></textarea>', body) is not None
      and "&lt;b&gt;sale&lt;/b&gt;" in a.get("/app/desk?ask=Four%20image%20ads%20for%20the%20%3Cb%3Esale%3C/b%3E").get_data(as_text=True),
      "a sentence handed over in the address is in the box, escaped")
long = page(a, "/app/desk?ask=" + "word%20" * 400)
check(len(re.search(r"data-ask>(.*?)</textarea>", long, re.S).group(1)) <= 600, "and capped at the box's length")

print("== on the Tools page")
body = page(a, "/app/tools")
sec = re.search(r'<section class="tp-insp" id="inspiration".*?</section>', body, re.S).group(0)
check("Ads made here, by Campaignify." in sec and sec.count("data-cat-pick=") == 7, "the Inspiration section, with the seven kinds that have examples")
links = re.findall(r'<a class="dk-ex" href="([^"]+)"', sec)
check(len(links) == len(cards) and all(x.startswith(("/app/studio?preset=", "/app/desk?ask=")) for x in links),
      "every card is a link: to Studio with its preset, or to the desk with its sentence")
check(all(a.get(x).status_code == 200 for x in links), "and every one opens")
b = signup("b@example.com")
with db.connect() as conn:
    import onboarding
    onboarding.finish(conn, user("b@example.com"))
    conn.commit()
sec_b = re.search(r'<section class="tp-insp" id="inspiration".*?</section>', page(b, "/app/tools"), re.S).group(0)
check("/app/desk?ask=" not in sec_b and 'href="/app/welcome/brand"' in sec_b, "with no product yet, a sentence card leads to the brand step")

print("== the brand step before a link is read")
start = page(b, "/app/welcome/brand")
check("What you'll get" in start and [x for x in re.findall(r'<span class="bb-get-cap">([^<]+)</span>', start)]
      == ["What you sell", "Brand kit", "Who buys it", "How you sound"], "four cards say what the read brings back")
check("made-up brand as an example" in start and "for you to approve before any ad says it" in start,
      "they are said to be an example, and what the page says still waits for approval")
check("What you'll get" not in page(a, "/app/welcome/brand"), "once the brand is there the cards are gone")

print("== new look off")
os.environ.pop("UI_V2", None)
check(a.get("/app/tools").status_code == 302 and "data-cat-pick" not in page(a, "/app"), "no chips, no Tools page")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} inspiration checks passed")

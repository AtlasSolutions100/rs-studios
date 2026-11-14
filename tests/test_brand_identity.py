"""Reading the brand when a link is imported (brand_identity.py; Koby, Nov 11 2026): the logo, colours, what the
brand is about and how it sounds go into the brand, its competitors are found, and on a paid plan the full research
starts by itself. Throwaway database; the site, the text model and the research are faked.

  * off (IMPORT_ENRICH=0): nothing happens
  * the import route queues it for the product's brand, and an import that failed queues nothing
  * identity: website, about, colours and logo from the site, with no model; the voice from one small call
  * nothing the member wrote is overwritten; words that break a claim rule are not kept
  * competitors: found once, saved on the brand; free plan: the list and a link to the priced research, and no
    research is started; paid plan: the full research starts by itself, free, on the closest three, once
  * caps: not again while it is running or within ENRICH_DAYS; past the daily cap only the part with no model runs
  * a site that can't be read changes nothing and raises nothing
  * the desk's brand panel shows the logo, the competitors, the state of the research and an angle to test

    python tests/test_brand_identity.py      (tests/test_app.py runs it too)
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
TMP = tempfile.mkdtemp(prefix="rs_enrich_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import copywriter  # noqa: E402
import desk  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import llm  # noqa: E402
import studio_v2  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png(w=400, h=400, rgb=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(b, "PNG")
    b.seek(0)
    return b


def no_vendor(*a, **kw):
    raise AssertionError("this test never calls a vendor")


submitted = []
hf.submit = lambda model, inputs, webhook_url=None, idempotency_key=None: submitted.append(model) or f"req{len(submitted)}"
hf.upload = lambda data, ctype: "https://files.example/1.png"
hf.status = lambda rid: {"status": "in_progress"}
hf.cancel = lambda rid: hf.STARTED
worker.requests.get = no_vendor
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (user(email)["workspace_id"],)).fetchone()


def member(email, name="Ember Candle", credits=20000):
    """A signed-up, onboarded, email-confirmed member with one product (with a photo) and credits to spend."""
    import onboarding
    import products
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.execute("UPDATE users SET email_verified_at=? WHERE email=?", (db.now(), email))
        conn.commit()
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": name, "photos": (png(), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM products WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                         (user(email)["workspace_id"],)).fetchone()
        if not conn.execute("SELECT 1 FROM photos WHERE product_id=?", (p["id"],)).fetchone():
            path = os.path.join(TMP, f"p-{p['id']}.png")
            open(path, "wb").write(png().getvalue())
            products.add_photo(conn, p["workspace_id"], p["id"], path, "p.png", "image/png")
        conn.execute("UPDATE products SET description=?, offer=?, claims=?, audience=? WHERE id=?",
                     ("A soy wax candle with amber and cedar.", "Free shipping this week",
                      "Burns for 40 hours\nHand poured in Texas", "People who light a candle every evening", p["id"]))
        if credits:
            db.adjust_credits(conn, p["workspace_id"], credits, "test top-up")
        conn.commit()
    return c, p["id"]





import brand_identity  # noqa: E402
import competitors  # noqa: E402
import fetcher  # noqa: E402
import product_import  # noqa: E402
import service_path  # noqa: E402
import tasks  # noqa: E402
from flask import jsonify  # noqa: E402


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def brand_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM brands WHERE workspace_id=? ORDER BY id LIMIT 1", (user(email)["workspace_id"],)).fetchone()


def reports(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM competitor_reports WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def set_brand(email, **cols):
    with db.connect() as conn:
        conn.execute(f"UPDATE brands SET {', '.join(k + '=?' for k in cols)} WHERE workspace_id=?", (*cols.values(), user(email)["workspace_id"]))
        conn.commit()


SITE = {"name": "Ember Candle Co.", "description": "Small-batch soy candles, hand poured in Austin.", "headline": "Candles for the evening",
        "text": "We pour every candle by hand in small batches. Soy wax, cotton wicks, scents that are easy to live with.",
        "colors": ["#c96f2d", "#2b2118"], "logo": "https://embercandle.example/logo.png"}
site, calls, started, found = [dict(SITE)], [], [], [[
    {"name": "Wick & Wax", "website": "https://wickandwax.example", "why": "Soy candles for gifts", "sources": ["https://a.example"]},
    {"name": "Slow Burn", "website": "https://slowburn.example", "why": "Small-batch candles", "sources": ["https://b.example"]},
    {"name": "Hearthlight", "website": "", "why": "Evening candles", "sources": ["https://c.example"]},
    {"name": "Fourth & Flame", "website": "https://fourth.example", "why": "Gift sets", "sources": ["https://d.example"]}]]


def read_site(url):
    calls.append(("site", url))
    if isinstance(site[0], Exception):
        raise site[0]
    return dict(site[0])


service_path.read_site = read_site
service_path.download_logo = lambda url: (calls.append(("logo", url)), (png(96, 96, (201, 111, 45)).getvalue(), "image/png"))[1]
llm.configured = lambda: True
llm.anthropic_ready = lambda: True
voice = [{"tone": "Calm, warm, a little dry", "audience": "People who light a candle most evenings"}]
llm.structured = lambda **kw: (calls.append(("voice", kw["prompt"])), voice[0])[1]
competitors.discover = lambda brand_text, usage, sources: (calls.append(("discover", brand_text)), [dict(x) for x in found[0]])[1]
competitors.start = lambda rid: started.append(rid) or "running"
URL = "https://embercandle.example/products/ember"

print("== off")
a, pid = member("a@example.com")
os.environ["IMPORT_ENRICH"] = "0"
ba = brand_of("a@example.com")
check(brand_identity.start(ba["workspace_id"], ba["id"], pid, URL) is False and not calls and not brand_of("a@example.com")["enrich_json"],
      "IMPORT_ENRICH=0: nothing is read")
os.environ.pop("IMPORT_ENRICH")

print("== the import queues it")
seen = []
real_start = brand_identity.start
brand_identity.start = lambda ws_id, bid, p, url: seen.append((ws_id, bid, p, url)) or True
appmod._start_import_page = lambda url: jsonify({"ok": True, "product_id": pid})
r = a.post("/app/start/import", data=json.dumps({"url": URL}), content_type="application/json", headers={"X-CSRF": csrf(a)})
check(r.status_code == 200 and seen == [(ba["workspace_id"], ba["id"], pid, URL)], "a link that imported has its brand read, for the product's own brand")
appmod._start_import_page = lambda url: (jsonify({"error": "Couldn't read that page.", "manual": True}), 422)
a.post("/app/start/import", data=json.dumps({"url": URL}), content_type="application/json", headers={"X-CSRF": csrf(a)})
check(len(seen) == 1, "an import that failed reads nothing")
brand_identity.start = real_start

print("== a free account")
set_brand("a@example.com", tone="", about="", website="", audience="")
check(brand_identity.start(ba["workspace_id"], ba["id"], pid, URL) is True, "the brand is read (inline here, queued in production)")
b1 = brand_of("a@example.com")
check(b1["website"] == "https://embercandle.example/" and b1["about"] == SITE["description"] and b1["kit_primary"] == "#c96f2d"
      and b1["kit_accent"] == "#2b2118" and ("site", "https://embercandle.example/") in calls, "the site's own home page gives the website, what it is about and the colours")
check(b1["kit_logo"] and os.path.exists(b1["kit_logo"]) and os.path.dirname(b1["kit_logo"]).endswith(str(ba["workspace_id"])),
      "its logo becomes the brand kit's logo, in the workspace's own folder")
check(b1["tone"] == "Calm, warm, a little dry" and b1["audience"] == "People who light a candle most evenings"
      and any(c[0] == "voice" and "Soy wax, cotton wicks" in c[1] for c in calls), "the voice and who it is for come from the page's own words")
check([x["name"] for x in brand_identity.rivals(b1)] == ["Wick & Wax", "Slow Burn", "Hearthlight", "Fourth & Flame"]
      and sum(1 for c in calls if c[0] == "discover") == 1, "its competitors are found once and kept on the brand")
st = json.loads(b1["enrich_json"])
check(st["state"] == "done" and {"website", "about", "kit_primary", "kit_logo", "tone", "rivals"} <= set(st["did"]) and "research" not in st["did"]
      and not reports("a@example.com") and not started, "free plan: no research is started")
d = page(a, "/app/desk?ui=v2") if False else None
os.environ["UI_V2"] = "1"
d = page(a, "/app/desk")
check("Your rivals" in d and "Wick & Wax" in d and 'href="https://wickandwax.example"' in d and "Hearthlight" in d
      and "Look at what they sell and say" in d and "See the price" in d and "/app/competitors" in d,
      "the desk lists them, with a link to the research where its price is shown")
check(f'/app/brands/{ba["id"]}/logo' in d and "Calm, warm, a little dry" in d, "and shows the logo and the voice it read")
check(brand_identity.start(ba["workspace_id"], ba["id"], pid, URL) is False, "not read again straight away")
st["at"] = (datetime.now(timezone.utc) - timedelta(days=brand_identity.ENRICH_DAYS + 1)).isoformat()
set_brand("a@example.com", enrich_json=json.dumps(st))
with db.connect() as conn:
    check(brand_identity.due(conn, ba["workspace_id"], ba["id"]), "read again after ENRICH_DAYS")
    set_brand("a@example.com", enrich_json=json.dumps({"state": "running", "at": db.now()}))
    check(not brand_identity.due(conn, ba["workspace_id"], ba["id"]), "not while a reading is going")
    set_brand("a@example.com", enrich_json=json.dumps({"state": "running", "at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()}))
    check(brand_identity.due(conn, ba["workspace_id"], ba["id"]), "a reading that died is taken up again")
check("Reading your site and finding your rivals" not in page(a, "/app/desk"), "a reading that died isn't shown as still going")

print("== what the member wrote stays")
b, pid_b = member("b@example.com")
bb = brand_of("b@example.com")
set_brand("b@example.com", tone="Loud and funny", kit_primary="#112233", about="We sell candles.", website="https://mine.example/")
site[0] = dict(SITE, description="Guaranteed to cure insomnia in one night.")
voice[0] = {"tone": "Earn $5,000 a month guaranteed", "audience": "Everyone"}
brand_identity.run(bb["workspace_id"], bb["id"], pid_b, URL)
b2 = brand_of("b@example.com")
check(b2["tone"] == "Loud and funny" and b2["kit_primary"] == "#112233" and b2["about"] == "We sell candles." and b2["website"] == "https://mine.example/",
      "a field the member filled in is never overwritten")
check(b2["kit_accent"] == "#2b2118" and b2["audience"] == "Everyone", "the empty ones are filled")
c, pid_c = member("c@example.com")
bc = brand_of("c@example.com")
set_brand("c@example.com", tone="", about="", audience="")
brand_identity.run(bc["workspace_id"], bc["id"], pid_c, URL)
b3 = brand_of("c@example.com")
check(not b3["about"] and not b3["tone"], "words that break a claim rule are not kept as the brand's own")
site[0], voice[0] = dict(SITE), {"tone": "Calm, warm, a little dry", "audience": "People who light a candle most evenings"}

print("== a paid plan")
p_, pid_p = member("p@example.com")
bp_ = brand_of("p@example.com")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (bp_["workspace_id"],))
    conn.commit()
out = brand_identity.run(bp_["workspace_id"], bp_["id"], pid_p, URL)
rs = reports("p@example.com")
inp = json.loads(rs[0]["inputs_json"])
check(len(rs) == 1 and started == [rs[0]["id"]] and out["report"] == rs[0]["id"] and rs[0]["status"] == "running",
      "the full research starts by itself")
check(inp["free"] is True and [x["name"] for x in inp["competitors"]] == ["Wick & Wax", "Slow Burn", "Hearthlight"] and inp["quote"] == competitors.quote(3),
      "on the closest three, free to the member, with the run's own cost limit")
check("Looking at what they sell and say" in page(p_, "/app/desk"), "the desk says the research is going")
brand_identity.run(bp_["workspace_id"], bp_["id"], pid_p, URL)
check(len(reports("p@example.com")) == 1 and len(started) == 1 and sum(1 for c in calls if c[0] == "discover") == 4,
      "once per brand: a later reading starts no second research and doesn't look for competitors again")
with db.connect() as conn:
    conn.execute("UPDATE competitor_reports SET status='ready', report_json=? WHERE id=?", (json.dumps({"synthesis": {"angles": [
        {"angle": "the easy gift", "hook": "For the person who has everything.", "why": "Two of three lead with gifting.", "sources": []}]}}), rs[0]["id"]))
    conn.commit()
d = page(p_, "/app/desk")
check("An idea to try" in d and "the easy gift" in d and 'data-try="A campaign for Ember Candle that leads with the easy gift"' in d
      and f"/app/competitors/{rs[0]['id']}" in d, "when it is ready the desk shows an angle to test, one click from a plan, and links the research")

t_, pid_t = member("t@example.com")
bt = brand_of("t@example.com")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='growth', plan_status='trialing' WHERE id=?", (bt["workspace_id"],))
    conn.commit()
    check(not brand_identity.paying(conn.execute("SELECT * FROM workspaces WHERE id=?", (bt["workspace_id"],)).fetchone())
          and brand_identity.paying(conn.execute("SELECT * FROM workspaces WHERE id=?", (bp_["workspace_id"],)).fetchone()),
          "a card trial that hasn't paid yet doesn't count as paying")
brand_identity.run(bt["workspace_id"], bt["id"], pid_t, URL)
check(not reports("t@example.com") and len(started) == 1 and brand_identity.rivals(brand_of("t@example.com")),
      "so a trial gets the competitors found, and the full research only at its price")

print("== caps and failures")
q, pid_q = member("q@example.com")
bq = brand_of("q@example.com")
set_brand("q@example.com", tone="", about="")
os.environ["ENRICH_DAILY"] = "0"
n = len(calls)
brand_identity.run(bq["workspace_id"], bq["id"], pid_q, URL)
b4 = brand_of("q@example.com")
check(b4["about"] == SITE["description"] and b4["kit_primary"] == "#c96f2d" and not b4["tone"] and not brand_identity.rivals(b4)
      and not [x for x in calls[n:] if x[0] in ("voice", "discover")], "past the daily cap the part with no model still runs, and nothing else")
os.environ.pop("ENRICH_DAILY")
r_, pid_r = member("r@example.com")
br = brand_of("r@example.com")
set_brand("r@example.com", about="", tone="x")
site[0] = product_import.Blocked(403, "page")
out = brand_identity.run(br["workspace_id"], br["id"], pid_r, URL)
b5 = brand_of("r@example.com")
check(not b5["about"] and not b5["kit_primary"] and json.loads(b5["enrich_json"])["state"] == "done", "a site that refuses apps is left alone and nothing breaks")
site[0] = fetcher.FetchError("timed out")
check(brand_identity.run(br["workspace_id"], br["id"], pid_r, URL)["did"] is not None, "nor does a site that can't be reached")
site[0] = dict(SITE)
check(brand_identity.origin("https://shop.example.com/products/x?y=1") == "https://shop.example.com/" and brand_identity.origin("example.com/a") == "https://example.com/"
      and brand_identity.origin("") == "", "the site is the link's own origin")
set_brand("r@example.com", enrich_json=json.dumps({"state": "running", "at": db.now()}))
ran = []
real_safe = brand_identity._safe_run
brand_identity._safe_run = lambda p: ran.append(p) or {}
with db.connect() as conn:
    check("brand_enrich" in tasks.KINDS and tasks.still_wanted(conn, "brand_enrich", {"brand": br["id"]}), "the worker knows the job and still wants one that is marked running")
tasks.run_task("brand_enrich", {"ws": br["workspace_id"], "brand": br["id"], "product": pid_r, "url": URL})
check(ran and ran[0]["brand"] == br["id"], "and runs it")
brand_identity._safe_run = real_safe

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} brand identity checks passed")

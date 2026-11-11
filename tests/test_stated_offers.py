"""Discounts, coupons and free shipping in ad words: allowed when the member stated the offer, refused when it was
made up (compliance.py "what the member has stated"; Koby, Nov 11 2026). On a throwaway database, every vendor and
the text model faked:

  * the rule alone: refused with nothing stated; passes on the offer's own terms when stated; a different number
    does not pass; state() covers work done outside a request and ends with unstate()
  * every other rule is untouched: stating an income claim does not make it pass
  * in the app: words the member typed themselves pass; the model's words pass when they repeat the member's
    sentence or the product's saved offer, and are left out (and named) when they invent an offer
  * the free hook generator: a visitor's own discount is fine, an invented one is dropped
  * a message the member saved is still sent with its offer in it

    python tests/test_stated_offers.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_stated_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "HOOK_TOOL", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import compliance  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import hooktool  # noqa: E402
import llm  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def no_vendor(*a, **kw):
    raise AssertionError("this test never calls a vendor")


hf.submit = lambda *a, **k: "req1"
hf.upload = lambda data, ctype: "https://files.example/1.png"
hf.status = lambda rid: {"status": "in_progress"}
worker.requests.get = no_vendor
ok = lambda *t, **kw: compliance.check(*t, **kw)["ok"]  # noqa: E731

print("== the rule alone")
r = compliance.check("20% off everything this week")
check(not r["ok"] and r["blocks"] == [compliance.DISCOUNT_MSG], "with nothing stated a discount is refused, and the message says how to state it")
check(all(not ok(t) for t in ("Free shipping today", "Use coupon GLOW", "promo code inside", "BOGO candles", "a big discount")),
      "so are free shipping, coupons, promo codes and BOGO")
check(ok("20% off everything this week", stated="Our offer: 20% off your first order"), "the stated offer passes")
check(not ok("30% off everything this week", stated="Our offer: 20% off your first order"), "a different number than the one stated does not")
check(ok("Get 20 % OFF today", stated="save 20 percent off") and ok("Free Shipping on every order", stated="free  shipping over $50"),
      "the match is on the offer's terms, not its spacing, case or 'percent'")
check(ok("20% off and free shipping", stated=("20% off", "free shipping")) and not ok("20% off and free shipping", stated="20% off"),
      "each offer in the words has to be stated")
check(not ok("Make $500 a week from home", stated="Make $500 a week from home")
      and not ok("Cures anxiety. 20% off", stated="Cures anxiety. 20% off"), "stating an income or health claim does not make it pass")
tok = compliance.state("Black Friday: 25% off")
check(ok("25% off, this weekend only") and not ok("40% off"), "state() covers work done outside a request")
compliance.unstate(tok)
check(not ok("25% off, this weekend only"), "and ends with unstate()")
check([f[1] for f in compliance.findings("20% off")] == ["discounts"] and compliance.findings("20% off", stated="20% off") == [],
      "findings() reads the same rule the same way")

print("== in the app")
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.7.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def jpost(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF": csrf(c)})


def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (400, 400), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


import onboarding  # noqa: E402
import products  # noqa: E402

EMAIL = "offers@example.com"
a = client()
assert a.post("/signup", data={"csrf": csrf(a), "email": EMAIL, "password": "longpassword"}).status_code == 302
with db.connect() as conn:
    u = conn.execute("SELECT * FROM users WHERE email=?", (EMAIL,)).fetchone()
    onboarding.finish(conn, u)
    conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
    conn.commit()
a.post("/app/products/new", data={"csrf": csrf(a), "name": "Ember Candle", "photos": (png(), "p.png")}, content_type="multipart/form-data")
with db.connect() as conn:
    p = conn.execute("SELECT * FROM products ORDER BY id DESC LIMIT 1").fetchone()
    if not conn.execute("SELECT 1 FROM photos WHERE product_id=?", (p["id"],)).fetchone():
        path = os.path.join(TMP, "p.png")
        open(path, "wb").write(png().getvalue())
        products.add_photo(conn, p["workspace_id"], p["id"], path, "p.png", "image/png")
    conn.execute("UPDATE products SET offer='Free shipping this week' WHERE id=?", (p["id"],))
    db.adjust_credits(conn, p["workspace_id"], 20000, "test top-up")
    conn.commit()
pid = p["id"]
os.environ["UI_V2"] = "1"
q = jpost(a, "/app/studio/quote", {"mode": "image", "product": pid, "text": "20% off every candle today", "count": 1}).get_json()
check(q.get("count") == 1 and not q.get("error"), "a headline the member typed with their own discount is priced, not refused")

calls, answer = [], [None]
llm.configured = lambda: True
llm.structured = lambda **kw: (calls.append(kw), answer[0])[1]
answer[0] = {"note": "Three.", "parts": [
    {"make": "image", "text": "Free shipping this week on every candle", "count": 1},
    {"make": "image", "text": "15% off for the holidays", "count": 1},
    {"make": "image", "text": "50% off everything, today only", "count": 1}]}
d = jpost(a, "/app/studio/ask", {"ask": "Image ads for our 15% off holiday sale", "product": pid}).get_json()
texts = [it["text"] for it in d["plan"]]
check(texts == ["Free shipping this week on every candle", "15% off for the holidays"],
      "the model's words stay when they repeat the product's saved offer or the member's own sentence")
check(len(d["skipped"]) == 1 and compliance.DISCOUNT_MSG in d["skipped"][0] and "50%" not in json.dumps(d),
      "the offer it made up is left out and named, without its words")
check("Offer: Free shipping this week" in calls[-1]["prompt"] and "Never make one up" in calls[-1]["system"],
      "the model is given the saved offer, and told never to invent or change one")
r = jpost(a, "/app/studio/ask", {"ask": "Tell people they will make $500 a week selling these", "product": pid})
check(r.status_code == 400 and "claim" in r.get_json()["error"], "an ask for an income claim is still refused")
os.environ.pop("UI_V2", None)

print("== the free hook generator")
hooktool._seen.clear()
answer[0] = {"hooks": [{"angle": "The offer", "text": "20% off your first bag this week."},
                       {"angle": "A lie", "text": "70% off everything, forever."},
                       {"angle": "The problem", "text": "Your coffee goes stale in a week."}]}
v = client()
page = v.get(hooktool.PATH).get_data(as_text=True)
import re  # noqa: E402
tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
html = v.post(hooktool.PATH, data={"csrf": tok, "product": "fresh roasted coffee, 20% off your first bag",
                                   "audience": "people who brew at home", "platform": "short", "website": ""}).get_data(as_text=True)
check("20% off your first bag this week." in html and "Your coffee goes stale in a week." in html and "70% off" not in html,
      "a visitor's own discount comes back in the hooks; one the model invented is dropped")
check("claim our ads can" not in html, "and the brief isn't refused for it")

print("== a message the member saved")
body = "Thanks for signing up. Use code WELCOME for 10% off your first order."
check(not ok(body) and ok(body, stated=body), "a saved email or text is sent with its own offer in it (the senders pass it as stated)")
src = open(os.path.join(ROOT, "messaging.py"), encoding="utf-8").read() + open(os.path.join(ROOT, "sms.py"), encoding="utf-8").read()
check(src.count("stated=") == 2, "both senders do")
imp = open(os.path.join(ROOT, "product_import.py"), encoding="utf-8").read()
check("compliance.check(offer, stated=page_text)" in imp and "Never invent one" in imp,
      "an offer the product page itself states is kept by the importer")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

"""Ask in Studio (studio_v2.ask, /app/studio/ask, the composer's first tab), on a throwaway database with the text
model and every vendor faked:

  * off (UI_V2 unset) the route is a 404; with no text model the tab isn't shown and asking says so
  * a sentence comes back as a set: each part named, with its words, priced by Create's own quote; nothing is made
    and nothing is charged by asking
  * the model sees the product's facts and approved claims and the ask as data; what it proposes is cut down to
    what Studio makes (unknown kinds, unknown presenters, words that trip a claim rule never reach the member)
  * nothing starts without the total the member saw; the confirmed total starts every ticked part in one batch
    named after the ask, and exactly that total is charged
  * unticking a part and editing its words re-prices; only the fields an ask may set are read back from the browser
  * a part Create can't price is left out and named; an ask with a blocked claim never reaches the model
  * the daily cap, a model error and an empty answer are each a sentence for the member, never a 500

    python tests/test_studio_ask.py      (tests/test_app.py runs it too)
"""
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
TMP = tempfile.mkdtemp(prefix="rs_studioask_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
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


def jobs_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def batches_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM batches WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def jpost(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF": csrf(c)})


def boot_of(html):
    return json.loads(re.search(r"window\.CFY_STUDIO = (\{.*?\});</script>", html, re.S).group(1))


def asks_today(email):
    with db.connect() as conn:
        return studio_v2.ask_used(conn, user(email)["workspace_id"])


# the text model, stubbed: `answer` is what it says next; `calls` is what it was asked
calls, answer = [], [None]


def fake_structured(**kw):
    calls.append(kw)
    if isinstance(answer[0], Exception):
        raise answer[0]
    return answer[0]


llm.structured = fake_structured
EMAIL = "ask@example.com"
ASK = "Launch the candle for the holidays: a few image ads and one short presenter video"

print("== off: no route, no tab")
a, pid = member(EMAIL)
check(jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid}).status_code == 404 and not calls,
      "with the new look off, asking is a 404 and the model is never called")

os.environ["UI_V2"] = "1"
page = a.get("/app").get_data(as_text=True)
boot = boot_of(page)
check(not boot["ask_on"] and boot["start"] == "" and 'data-mode="ask"' not in page,
      "with no text model the Ask tab isn't shown and Studio starts where it did")
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
check(r.status_code == 503 and "composer" in r.get_json()["error"] and not calls, "and asking says to use the composer")

print("== on: the tab, and where Studio starts")
llm.configured = lambda: True
page = a.get("/app").get_data(as_text=True)
boot = boot_of(page)
check(boot["ask_on"] and boot["urls"]["ask"] == "/app/studio/ask" and 'data-mode="ask"' in page
      and page.index('data-mode="ask"') < page.index('data-mode="image"'), "the Ask tab is the composer's first")
check(boot["first_run"] and boot["start"] == "", "a first run still opens on its two free image ads")
check("data-q-plan" in page and "data-q-again" in page and "data-asking" in page,
      "the price box has the place for a set, and the composer the place that says it is working")
js = open(os.path.join(ROOT, "static", "studio_v2.js"), encoding="utf-8").read()
check("post(B.urls.ask," in js and js.count("post(B.urls.generate,") == 1
      and re.search(r"\$\('\[data-confirm-make\]'\)\.addEventListener\('click', function\(e\)\{ if\(!lastQuote\) return; generate\(", js),
      "studio_v2.js sends an ask to /ask, and still starts things only from the price box's button")

print("== a sentence in, a priced set back")
with appmod.app.test_request_context("/app"):
    cast = studio_v2.roster()
who = cast[0]
place = (who.get("situations") or [{}])[0].get("id") if who.get("situations") else None
answer[0] = {"note": "Three headlines to test and one short video to carry the offer.", "parts": [
    {"make": "image", "text": "Your whole evening, in one candle.", "count": 3},
    {"make": "presenter", "text": "This is the Ember Candle. Soy wax, amber and cedar, hand poured in Texas.",
     "presenter": who["id"], "situation": place or "nowhere"},
    {"make": "product_video", "text": "The candle on a wooden table, the flame flickering in warm evening light."},
    {"make": "blog_post", "text": "Ten reasons to love candles"},
    {"make": "image", "text": "Make $500 a week selling candles from home", "count": 1},
]}
bal = db.balance(ws_of(EMAIL))
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
d = r.get_json()
check(r.status_code == 200 and [it["make"] for it in d["plan"]] == ["image", "presenter", "product_video"],
      "the set: what Studio makes, in the order proposed; an unknown kind and a blocked claim are dropped")
check(len(d["skipped"]) == 1 and d["skipped"][0].startswith("Image ad: the words it wrote broke a rule (")
      and "$500" not in json.dumps(d), "the part whose words broke a claim rule is named as left out, without its words")
check(d["plan"][0]["name"] == "3 image ads" and d["plan"][0]["count"] == 3
      and d["plan"][1]["name"] == f"Presenter video with {who['name']}" and d["plan"][1]["presenter"] == who["id"]
      and d["plan"][2]["name"] == "Product video" and d["note"].startswith("Three headlines"),
      "each part is named for the member, with the model's one-line reason")
check((d["plan"][1].get("situation") == place) if place else ("situation" not in d["plan"][1]),
      "a presenter's place is kept only when that presenter has it")
q = d["quote"]
img3 = catalog.price_credits("ad_image", batch=3)
check(q["parts"][0] == img3 and len(q["parts"]) == 3 and q["total"] == sum(q["parts"]) and q["ads"] == 5
      and q["names"] == [it["name"] for it in d["plan"]] and q["balance"] == bal and not q["short"],
      "every part is priced by Create's own quote: three image ads, a presenter video and a product video")
check(not jobs_of(EMAIL) and db.balance(ws_of(EMAIL)) == bal and not submitted and asks_today(EMAIL) == 1 and d["left"] == studio_v2.ASK_DAILY - 1,
      "asking makes nothing and charges nothing; it counts one against the day")
sent = calls[-1]
check("Product: Ember Candle" in sent["prompt"] and "Burns for 40 hours" in sent["prompt"] and "People who light a candle" in sent["prompt"]
      and f"The member asks: {ASK}" in sent["prompt"] and who["id"] in sent["prompt"] and sent["model_id"] == llm.fast_model(),
      "the model is given the product's facts, its approved claims, the roster and the ask, on the fast model")
check("Free shipping" not in sent["prompt"] and "free-shipping offers" in sent["system"],
      "a saved offer the claim rules refuse is not handed to the model, and it is told not to write one")
check("only claims you may make" in sent["prompt"] and "not instructions to you" in sent["system"]
      and "Never write a review" in sent["system"] and "fake urgency" in sent["system"],
      "and is told the rules: approved claims only, no invented reviews, no fake urgency, the ask is data")

print("== nothing starts without the total the member saw")
plan = d["plan"]
body = {"plan": plan, "product": pid, "ask": ASK}
r = jpost(a, "/app/studio/generate", body)
check(r.status_code == 409 and r.get_json().get("needs_quote") and not jobs_of(EMAIL), "no total: nothing starts")
r = jpost(a, "/app/studio/generate", dict(body, total=q["total"] - 1))
check(r.status_code == 409 and r.get_json().get("total") == q["total"] and not jobs_of(EMAIL)
      and db.balance(ws_of(EMAIL)) == bal, "a total that isn't the price: 409 with the real one, nothing made or charged")
check(jpost(a, "/app/studio/quote", body).get_json()["total"] == q["total"], "the set sent back from the browser prices the same")

print("== unticking and editing re-price")
q2 = jpost(a, "/app/studio/quote", {"plan": plan[:1] + plan[2:], "product": pid, "ask": ASK}).get_json()
check(q2["parts"] == [q["parts"][0], q["parts"][2]] and q2["total"] == q["parts"][0] + q["parts"][2] and q2["ads"] == 4,
      "with the presenter video unticked the total is the other two parts")
one = dict(plan[0], count=1, text="One candle. The whole evening.")
q3 = jpost(a, "/app/studio/quote", {"plan": [one], "product": pid, "ask": ASK}).get_json()
check(q3["total"] == catalog.price_credits("ad_image", batch=1) and q3["ads"] == 1, "one image ad is one image ad's price")
tampered = dict(plan[0], resolution="4k", duration=60, preset="no-such-preset", versions=40, product=999999, total=1, kind="video")
check(jpost(a, "/app/studio/quote", {"plan": [tampered], "product": pid}).get_json()["total"] == q["parts"][0],
      "only the fields an ask may set are read back: extra keys on a part change nothing")
nothing = jpost(a, "/app/studio/quote", {"plan": [], "product": pid}).get_json()
check(nothing.get("count") == 0 and nothing.get("error") == "Tick at least one thing to make.",
      "with nothing ticked there is nothing to price")
junk = jpost(a, "/app/studio/quote", {"plan": [{"make": "ugc", "text": "x"}, "nope", {"make": "image", "text": " "}], "product": pid}).get_json()
check(junk.get("count") == 0 and junk.get("error") == "Tick at least one thing to make.", "parts that aren't something Studio makes are not priced")
many = jpost(a, "/app/studio/quote", {"plan": [dict(one, text=f"Headline number {i}.") for i in range(9)], "product": pid}).get_json()
check(many["count"] == studio_v2.ASK_MAX_PARTS, f"a set is never more than {studio_v2.ASK_MAX_PARTS} parts")
bad = jpost(a, "/app/studio/quote", {"plan": [dict(one, text="Make $500 a week selling candles from home")], "product": pid}).get_json()
check(bad.get("count") == 0 and bad.get("error"), "words edited into a blocked claim are refused by Create's own rules")

print("== the confirmed total starts the ticked parts, in one batch")
edited = [dict(plan[0], text="Amber, cedar and a quiet evening."), plan[1], plan[2]]
body = {"plan": edited, "product": pid, "ask": ASK}
qe = jpost(a, "/app/studio/quote", body).get_json()
r = jpost(a, "/app/studio/generate", dict(body, total=qe["total"]))
g = r.get_json()
jobs = jobs_of(EMAIL)
check(r.status_code == 200 and g.get("ok") and "/app/studio?made=" in g["url"], "the confirmed total starts it and comes back to Studio")
check(sorted(j["tool"] for j in jobs).count("ad_image") >= 1 and len({j["tool"] for j in jobs}) == 3 and g["queued"] == len(jobs),
      "image ads, a presenter video and a product video are queued")
heads = [json.loads(j["params_json"]).get("headline") for j in jobs if j["tool"] == "ad_image"]
check(heads and all(h == "Amber, cedar and a quiet evening." for h in heads), "the image ads carry the words as the member left them")
bs = batches_of(EMAIL)
cfg = json.loads(bs[0]["config_json"])
check(len(bs) == 1 and bs[0]["id"] == g["batch"] and bs[0]["title"] == "Launch the candle for the holidays: a few image ads and…"
      and bs[0]["credits"] == qe["total"]
      and all(j["batch_id"] == bs[0]["id"] for j in jobs) and cfg.get("ask") == ASK
      and cfg.get("templates") == ["image", "presenter", "product_video"], "one batch, named after the ask, holding all of it")
check(db.balance(ws_of(EMAIL)) == bal - qe["total"], "exactly the shown total is charged")
page = a.get("/app").get_data(as_text=True)
check(boot_of(page)["start"] == "ask" and re.search(r'aria-checked="true"[^>]*data-mode="ask"', page),
      "after the first run Studio opens on Ask")
check(boot_of(a.get("/app?mode=image").get_data(as_text=True))["start"] == ""
      and boot_of(a.get(f"/app?batch={bs[0]['id']}").get_data(as_text=True))["start"] == "",
      "unless the member arrived with something to edit (a mode or a batch)")

print("== what an ask refuses")
n = len(calls)
r = jpost(a, "/app/studio/ask", {"ask": "ads", "product": pid})
check(r.status_code == 400 and "in a sentence" in r.get_json()["error"], "a few letters isn't an ask")
r = jpost(a, "/app/studio/ask", {"ask": "Tell people they will make $500 a week selling these candles", "product": pid})
check(r.status_code == 400 and r.get_json()["error"].startswith("That asks for a claim your ads can't make") and len(calls) == n,
      "an ask for a blocked claim is refused with the rule's message and never reaches the model")
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": None})
check(r.status_code == 400 and "product" in r.get_json()["error"] and len(calls) == n, "no product: add one first")
b, other = member("other@example.com", "Glow Serum")
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": other})
check(r.status_code == 400 and len(calls) == n and asks_today(EMAIL) == 1, "another workspace's product is not a product")

print("== a part that can't be priced is left out and named")
answer[0] = {"note": "A headline and a long script.", "parts": [
    {"make": "image", "text": "Light it when the day ends."},
    {"make": "presenter", "text": "word " * 400, "presenter": "nobody-by-this-name"}]}
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
d = r.get_json()
check(r.status_code == 200 and [it["make"] for it in d["plan"]] == ["image"] and len(d["skipped"]) == 1
      and d["skipped"][0].startswith("Presenter video with") and d["quote"]["total"] == catalog.price_credits("ad_image", batch=1),
      "the script that is too long is left out with Create's reason, and the rest is still priced")
check(who["name"] in d["skipped"][0], "an unknown presenter falls back to the first on the roster")
answer[0] = {"note": "", "parts": [{"make": "presenter", "text": "word " * 400}]}
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
check(r.status_code == 400 and r.get_json()["error"].startswith("Presenter video with"), "when no part can be priced the answer is the reason")

print("== a model that fails, says nothing, or is asked too often")
used = asks_today(EMAIL)
answer[0] = RuntimeError("the model is down")
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
check(r.status_code == 502 and "Try again" in r.get_json()["error"] and asks_today(EMAIL) == used, "a model error is a sentence, and isn't counted")
answer[0] = {"note": "Nothing.", "parts": []}
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
check(r.status_code == 422 and "image ads" in r.get_json()["error"], "an empty answer says what to name")
answer[0] = {"note": "A deal.", "parts": [{"make": "image", "text": "20% off every candle this week"}]}
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
check(r.status_code == 422 and "broke a rule" in r.get_json()["error"] and "20%" not in r.get_json()["error"],
      "when every part broke a claim rule the answer says which rule, not the words")
answer[0] = "not an object"
check(jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid}).status_code == 422, "an answer that isn't an object is an empty answer")
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (studio_v2._ask_key(user(EMAIL)["workspace_id"]), str(studio_v2.ASK_DAILY)))
    conn.commit()
n = len(calls)
r = jpost(a, "/app/studio/ask", {"ask": ASK, "product": pid})
check(r.status_code == 429 and str(studio_v2.ASK_DAILY) in r.get_json()["error"] and len(calls) == n,
      "the daily cap says so and the model isn't called")
check(jpost(b, "/app/studio/ask", {"ask": ASK, "product": other}).status_code == 422, "the cap is each workspace's own")

print("== talking videos switched off")
real = studio_v2.available_modes
studio_v2.available_modes = lambda: [m for m in studio_v2.MODES if m["id"] in ("image", "product_video")]
answer[0] = {"note": "A headline and a video.", "parts": [{"make": "image", "text": "Light it when the day ends."},
                                                         {"make": "presenter", "text": "This is the Glow Serum."},
                                                         {"make": "voiceover", "text": "This is the Glow Serum."}]}
d = jpost(b, "/app/studio/ask", {"ask": ASK, "product": other}).get_json()
check([it["make"] for it in d["plan"]] == ["image"] and "Kinds available: image, product_video" in calls[-1]["prompt"],
      "the model is told what can be made today, and anything else it proposes is dropped")
studio_v2.available_modes = real

os.environ.pop("UI_V2", None)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

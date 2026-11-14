"""The agent's questions before a plan and the Mac's light-model worker (light.py, desk.clarify, deploy/mac-light).
On a throwaway database; the Mac worker and Groq are faked, and Claude is never called.

    python tests/test_light.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_light_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER", "LIGHT_WORKER_TOKEN"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import copywriter  # noqa: E402
import desk  # noqa: E402
import light  # noqa: E402
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






def jpost(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF": csrf(c)})


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def wpost(path, body=None, token="tok-light-test"):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return appmod.app.test_client().post(path, data=json.dumps(body or {}), content_type="application/json", headers=h)


def jobs():
    with db.connect() as conn:
        light.ensure_table(conn)
        return conn.execute("SELECT * FROM light_jobs ORDER BY id").fetchall()


GOOD = {"questions": [
    {"q": "Where will people see it?", "options": [
        {"label": "Instagram Reels", "hint": "Short vertical video", "recommended": True},
        {"label": "TikTok", "hint": "Fast cuts", "recommended": True},
        {"label": "tiktok", "hint": "a repeat"},
        {"label": "Guaranteed income of $5,000 a month", "hint": "a claim the rules refuse"}]},
    {"q": "Cure your anxiety with candles?", "options": [{"label": "Yes"}, {"label": "No"}]},
    {"q": "Which scent leads?", "options": [{"label": "Amber"}, {"label": "Cedar"}]},
    {"q": "One option only", "options": [{"label": "Just this"}]},
    {"q": "A fourth question?", "options": [{"label": "A"}, {"label": "B"}]},
    {"q": "A fifth question?", "options": [{"label": "A"}, {"label": "B"}]}]}

os.environ["UI_V2"] = "1"
a, pid = member("a@example.com")

print("== no worker, no Groq: the rules ask")
check(light.worker() is None and "rules only" in light.status(), "with no token there is no worker")
check(wpost("/_light/next").status_code == 404, "and the worker's address isn't there")
vague = "Something for the holidays"
r = jpost(a, "/app/desk/clarify", {"ask": vague, "product": pid})
qs = r.get_json()["questions"]
check(r.status_code == 200 and r.get_json()["source"] == "rules" and [q["q"] for q in qs] ==
      ["Where will these ads run?", "What should I make first?", "What's the goal?"], "a vague sentence gets the three questions it leaves open")
check(all(2 <= len(q["options"]) <= 4 and sum(o["recommended"] for o in q["options"]) == 1 for q in qs),
      "each with two to four options and one recommended")
r = jpost(a, "/app/desk/clarify", {"ask": "Three TikTok image ads to drive sales of the candle", "product": pid})
check(r.get_json()["questions"] == [], "a sentence that says where, what and why gets no questions")
check(jpost(a, "/app/desk/clarify", {"ask": "x", "product": pid}).status_code == 400, "too short to ask about")
check(jpost(a, "/app/desk/clarify", {"ask": vague, "product": 99999}).status_code == 400, "another product isn't asked about")
check(a.post("/app/desk/clarify", data=json.dumps({"ask": vague, "product": pid}), content_type="application/json").status_code == 400,
      "the member's call needs the page's CSRF token")
check(jpost(a, "/app/desk/clarify", {"ask": "Ads that promise a guaranteed income of $5,000 a month", "product": pid}).get_json()["questions"] == [],
      "a claim the ads can't make gets no questions (the plan says why)")

print("== the worker's calls")
os.environ["LIGHT_WORKER_TOKEN"] = "tok-light-test"
check(wpost("/_light/next", token="").status_code == 401 and wpost("/_light/next", token="wrong").status_code == 401,
      "no token or a wrong one is refused")
check(light.worker() is None, "a refused call isn't counted as the worker being there")
r = wpost("/_light/next", {"model": "qwen3:14b", "by": "mac-mini", "wait": 0})
check(r.status_code == 204, "an empty queue: 204")
check((light.worker() or {}).get("model") == "qwen3:14b" and "Mac worker on (qwen3:14b)" in light.status(), "and the worker is seen, with its model")
check(not wpost("/_light/done/999", {"result": {"x": 1}}).get_json()["ok"], "an answer to a job it didn't take changes nothing")

print("== a question on the Mac")
answered = []


def fake_mac(result=None, error=""):
    r = wpost("/_light/next", {"model": "qwen3:14b", "by": "mac-mini", "wait": 5})
    if r.status_code == 200:
        job = r.get_json()
        answered.append(job)
        wpost(f"/_light/done/{job['id']}", {"result": result, "error": error})


t = threading.Thread(target=fake_mac, args=(GOOD,))
t.start()
r = jpost(a, "/app/desk/clarify", {"ask": vague, "product": pid})
t.join()
out = r.get_json()
check(out["source"] == "mac" and len(answered) == 1 and answered[0]["kind"] == "clarify", "the Mac took the job and answered")
job = answered[0]
check("Ember Candle" in job["prompt"] and vague in job["prompt"] and "Free shipping this week" in job["prompt"]
      and job["schema"] == desk.CLARIFY_SCHEMA and "multiple-choice" in job["system"], "it was sent the product, the request and the shape to answer in")
qs = out["questions"]
check([q["q"] for q in qs] == ["Where will people see it?", "Which scent leads?", "A fourth question?"],
      "a question the claim rules refuse, or with one option, is dropped; three at most")
check([o["label"] for o in qs[0]["options"]] == ["Instagram Reels", "TikTok"] and [o["recommended"] for o in qs[0]["options"]] == [True, False],
      "repeats and refused options are dropped, and only one is recommended")
check(all(not o["recommended"] or i == 0 for q in qs for i, o in enumerate(q["options"])) and qs[1]["options"][0]["recommended"],
      "with none marked, the first is recommended")
check(jobs()[-1]["status"] == "done", "the job is done")

t = threading.Thread(target=fake_mac, kwargs={"error": "ValueError: not JSON"})
t.start()
r = jpost(a, "/app/desk/clarify", {"ask": vague, "product": pid})
t.join()
check(r.get_json()["source"] == "rules" and len(r.get_json()["questions"]) == 3 and jobs()[-1]["status"] == "failed",
      "the Mac failing: the rules ask instead, at once")

got, where = light.ask_json("clarify", "s", "p", {"type": "object"}, wait=1)
check(got is None and where == "" and jobs()[-1]["status"] == "expired", "nobody taking it in time: no answer, and the job is expired")
check(wpost("/_light/next", {"wait": 0}).status_code == 204, "an expired job isn't handed out later")
with db.connect() as conn:
    conn.execute("INSERT INTO light_jobs (kind, prompt_json, created_at) VALUES ('clarify', '{}', ?)", (light._ago(120),))
    conn.commit()
check(wpost("/_light/next", {"wait": 0}).status_code == 204 and jobs()[-1]["status"] == "expired", "nor is one that waited too long")

print("== Groq when the Mac isn't there")
with db.connect() as conn:
    conn.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps({"at": light._ago(600), "model": "x"}), light.SEEN_KEY))
    conn.commit()
check(light.worker() is None, "a worker not seen lately isn't there")
os.environ["GROQ_API_KEY"] = "gsk_test"
sent = []
llm._groq = lambda body: (sent.append(body), {"choices": [{"message": {"content": json.dumps(GOOD)}}]})[1]
llm.chat = no_vendor           # Claude is never called for the questions
n = len(jobs())
r = jpost(a, "/app/desk/clarify", {"ask": vague, "product": pid}).get_json()
check(r["source"] == "groq" and len(r["questions"]) == 3 and len(sent) == 1 and len(jobs()) == n,
      "Groq answers, with no job queued for a Mac that isn't there")
llm._groq = lambda body: (_ for _ in ()).throw(llm.LLMError("503"))
check(jpost(a, "/app/desk/clarify", {"ask": vague, "product": pid}).get_json()["source"] == "rules", "Groq down: the rules")
llm._groq = lambda body: {"choices": [{"message": {"content": json.dumps({"questions": []})}}]}
check(jpost(a, "/app/desk/clarify", {"ask": vague, "product": pid}).get_json() == {"questions": [], "source": "groq"},
      "a model that sees nothing to ask asks nothing")
os.environ.pop("GROQ_API_KEY")

print("== the answers go to the plan")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (user("a@example.com")["workspace_id"],))
    conn.commit()
calls = []
llm.configured = lambda: True
llm.structured = lambda **kw: (calls.append(kw), {"note": "Two looks.", "parts": [{"make": "image", "text": "Amber evenings", "count": 2}]})[1]
answers = [{"q": "Where will these ads run?", "a": "TikTok"}, {"q": "What's the goal?", "a": "Sign-ups"}, {"q": "", "a": "dropped"}]
p = jpost(a, "/app/desk/plan", {"ask": vague, "product": pid, "answers": answers}).get_json()
check(len(calls) == 1 and "Something for the holidays Answers: Where will these ads run? TikTok. What's the goal? Sign-ups." in calls[0]["prompt"],
      "the plan is made from the sentence and the answers")
check(any(x["kind"] == "page" and x["on"] for x in p["parts"]), "an answer can ask for a part (sign-ups: the page)")
with db.connect() as conn:
    said = json.loads(conn.execute("SELECT body_json FROM desk_turns WHERE thread_id=? AND kind='said'", (p["thread"],)).fetchone()[0])
check(said == {"text": vague, "answers": answers[:2]}, "what they typed and what they picked are kept apart")
th = page(a, f"/app/desk/c/{p['thread']}")
check('<dl class="dk-qa"><dt>Where will these ads run?</dt><dd>TikTok</dd>' in th, "and the campaign shows the answers under the request")
p2 = jpost(a, "/app/desk/plan", {"ask": "Add a presenter video too", "product": pid, "thread": p["thread"], "answers": answers}).get_json()
check("Answers:" not in calls[-1]["prompt"], "a later turn takes no answers")
check(len(desk.with_answers("x" * 600, answers[:2])) == 600 and desk.with_answers("x" * 600, answers[:2]).endswith("Sign-ups."),
      "a long sentence is cut so the answers still fit")

d = page(a, "/app/desk")
boot = json.loads(re.search(r"window\.CFY_DESK = (\{.*?\});</script>", d, re.S).group(1))
check(boot["urls"]["clarify"] == "/app/desk/clarify" and "data-clarify" in d and "data-working" in d, "the desk page has the question card's place")

print(f"\nall {passed} light checks passed")
shutil.rmtree(TMP, ignore_errors=True)

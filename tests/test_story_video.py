"""Story ads (story_video.py) and the desk's ready-made jobs, on a throwaway database. The vendor and the writer are
faked; the stitch is real ffmpeg on tiny pictures when it is installed.

    python tests/test_story_video.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_story_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER", "LIGHT_WORKER_TOKEN"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import copywriter  # noqa: E402
import desk  # noqa: E402
import story_video  # noqa: E402
import stitch  # noqa: E402
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


FFMPEG = stitch.have() and os.environ.get("RS_TEST_NO_FFMPEG") != "1"
if not stitch.have():
    stitch.have = lambda: True


def pngb(size=(540, 960), color=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


_clips = {}


def clip(seconds):
    if seconds not in _clips:
        if not FFMPEG:
            _clips[seconds] = b"0" * 2000
        else:
            path = os.path.join(TMP, f"c{seconds}.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x2266cc:s=180x320:r=24:d={seconds}",
                            "-pix_fmt", "yuv420p", path], check=True)
            _clips[seconds] = open(path, "rb").read()
    return _clips[seconds]


class Vendor:
    def __init__(self):
        self.submits, self.files, self.plan, self.uploads = [], {}, {}, 0

    def upload(self, data, ctype):
        self.uploads += 1
        return f"https://files.example/up{self.uploads}"

    def submit(self, model, inputs, webhook_url=None, idempotency_key=None):
        self.submits.append({"model": model, "inputs": inputs, "key": idempotency_key})
        return f"req-{len(self.submits)}"

    def status(self, rid):
        n = int(rid.split("-")[1])
        if n in self.plan:
            return self.plan[n]
        sub = self.submits[n - 1]
        if "video" in sub["model"]:
            url = f"https://cdn.example/{rid}.mp4"
            self.files[url] = clip(int(sub["inputs"]["duration"]))
            return {"status": "completed", "video": {"url": url}}
        url = f"https://cdn.example/{rid}.png"
        self.files[url] = pngb(color=(40 * n % 255, 120, 60))
        return {"status": "completed", "images": [{"url": url}]}


V = Vendor()
hf.upload, hf.submit, hf.status = V.upload, V.submit, V.status
hf.cancel = lambda rid: hf.CANCELED
hf.estimate = lambda model, inputs: None


class _Got:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


worker.requests.get = lambda url, timeout=0, **kw: _Got(V.files[url])
if not FFMPEG:
    worker.make_poster = lambda path: None

STORY = {"character": "Bob, a cheerful man in his forties with a beard, a red apron and a grey T-shirt",
         "scenes": [{"caption": "THIS IS BOB.", "picture": "Bob in his kitchen, bored", "motion": "he sighs"},
                    {"caption": "BOB IS TIRED.", "picture": "Bob yawning at 3 pm at his desk", "motion": "he yawns"},
                    {"caption": "THEN BOB FOUND IT.", "picture": "Bob holding the green can, surprised", "motion": "his eyes go wide"},
                    {"caption": "BE LIKE BOB.", "picture": "Bob smiling with the can", "motion": "he winks"}],
         "cta": "Try Citrus Volt"}
ADS = {"note": "Here is the story.", "parts": []}
said = []


def fake_structured(**kw):
    said.append(kw)
    if "scenes" in json.dumps(kw.get("schema") or {}):
        return json.loads(json.dumps(STORY))
    return ADS


llm.structured = fake_structured
llm.configured = lambda: True
os.environ["UI_V2"] = "1"
a, pid = member("a@example.com", name="Citrus Volt", credits=20000)
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (user("a@example.com")["workspace_id"],))
    conn.commit()

print("== price and planning")
import math  # noqa: E402
check(story_video.price("photo", 5) == math.ceil(round(story_video.cost_usd("photo", 5) * catalog.markup_for("story_video") * 100, 6))
      and story_video.price("animated", 3) > story_video.price("photo", 6), "a story is priced from what it costs us; animated costs more")
check(story_video.price("photo", 9) == story_video.price("photo", 6) and story_video.price("photo", 1) == story_video.price("photo", 3),
      "three to six scenes")

print("== the agent plans a story ad")
p = jpost(a, "/app/desk/plan", {"ask": "A funny story ad for Citrus Volt, be like Bob", "product": pid}).get_json()
st = [x for x in p["parts"] if x["kind"] == "story"]
check(len(st) == 1 and st[0]["on"] and st[0]["style"] == "photo" and st[0]["name"] == "Funny story ad"
      and st[0]["price"] == story_video.price("photo", 5) and st[0]["label"] == f"{st[0]['price']:,} credits",
      "a funny story ad is a part of the plan, priced before anything is made")
p2 = jpost(a, "/app/desk/plan", {"ask": "An animated story ad for Citrus Volt, a 3D cartoon", "product": pid}).get_json()
s2 = [x for x in p2["parts"] if x["kind"] == "story"][0]
check(s2["style"] == "animated" and s2["name"] == "Animated story ad" and s2["scenes"] == 3, "the word animated picks the cartoon look")
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": [dict(st[0], scenes=4)]}).get_json()
check(q["parts"][0]["price"] == story_video.price("photo", 4) and q["total"] == story_video.price("photo", 4), "fewer scenes, a lower price")
q0 = jpost(a, "/app/desk/quote", {"product": pid, "parts": [dict(st[0], text="")]}).get_json()
check(not q0["parts"][0]["on"] and q0["parts"][0]["label"] == "Add the idea", "a story with no idea waits, unticked")

print("== making it")
bal0 = db.balance(ws_of("a@example.com"))
part = dict(st[0], scenes=4)
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": [part]}).get_json()
r = jpost(a, "/app/desk/run", {"product": pid, "parts": [part], "total": q["total"], "ask": "be like Bob"}).get_json()
check(r.get("ok") and r["made"][0]["kind"] == "story" and "/app/library" in r["made"][0]["href"], "it starts and points at the Library")
with db.connect() as conn:
    job = conn.execute("SELECT * FROM jobs WHERE tool='story_video' ORDER BY id DESC LIMIT 1").fetchone()
pj = json.loads(job["params_json"])
check(job["credits"] == q["total"] and db.balance(ws_of("a@example.com")) == bal0 - q["total"], "the credits shown are the credits taken")
check(pj["story"]["character"].startswith("Bob") and [s["caption"] for s in pj["story"]["scenes"]][0] == "THIS IS BOB."
      and pj["story"]["style"] == "photo", "the story was written by the model and kept on the job")
sys_prompt = said[-1]["system"]
check("invented adult" in sys_prompt and "never promise results" in sys_prompt, "the writer is told: a made-up adult, no promises")

for _ in range(12):
    worker.tick()
    with db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    if job["status"] in ("done", "failed"):
        break
check(job["status"] == "done", f"the story is made ({job['status']}: {job['error']})")
stills = [s for s in V.submits if "video" not in s["model"]]
check(len(stills) == 4 and not stills[0]["inputs"]["image_urls"][0].startswith("https://cdn.example")
      and all(s["inputs"]["image_urls"][0].startswith("https://cdn.example/") or s["inputs"]["image_urls"][0].startswith("https://files.example/")
              for s in stills[1:]) and "same person" in stills[1]["inputs"]["prompt"],
      "the first scene is drawn first; every other scene uses it, so it is the same person")
check(all("Bob, a cheerful man" in s["inputs"]["prompt"] and "No words" in s["inputs"]["prompt"] for s in stills),
      "each picture is told who the person is and that no words go in it")
with db.connect() as conn:
    outs = conn.execute("SELECT * FROM outputs WHERE job_id=?", (job["id"],)).fetchall()
check(len(outs) == 1 and outs[0]["kind"] == "video" and os.path.exists(outs[0]["path"]), "one video is delivered")
if FFMPEG:
    info = stitch.probe(outs[0]["path"])
    want = 4 * story_video.PHOTO_SECONDS + story_video.END_SECONDS
    check(abs(info["duration"] - want) < 1.0 and (info["width"], info["height"]) == (1080, 1920),
          f"four scenes and the end card, tall ({info['duration']:.1f} s)")
lib = page(a, "/app/library")
check("Story ad" in lib, "the Library card names it a story ad")

print("== a scene that fails twice: the whole story is refunded")
n0 = len(V.submits)
part = dict(st[0], scenes=3)
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": [part]}).get_json()
bal1 = db.balance(ws_of("a@example.com"))
V.plan = {n0 + 1: {"status": "failed"}, n0 + 2: {"status": "failed"}}
STORY_SHORT = dict(STORY, scenes=STORY["scenes"][:3])
llm.structured = lambda **kw: json.loads(json.dumps(STORY_SHORT)) if "scenes" in json.dumps(kw.get("schema") or {}) else ADS
r = jpost(a, "/app/desk/run", {"product": pid, "parts": [part], "total": q["total"], "ask": "x"}).get_json()
check(r.get("ok"), "it starts")
for _ in range(10):
    worker.tick()
with db.connect() as conn:
    job2 = conn.execute("SELECT * FROM jobs WHERE tool='story_video' ORDER BY id DESC LIMIT 1").fetchone()
check(job2["status"] == "failed" and db.balance(ws_of("a@example.com")) == bal1, "made again once, then refunded in full")

print("== a story the writer can't make")
llm.structured = lambda **kw: {"character": "Bob", "scenes": [{"caption": "LOSE 10 POUNDS", "picture": "Bob on a scale"}] * 3}
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": [part]}).get_json()
bal2 = db.balance(ws_of("a@example.com"))
r = jpost(a, "/app/desk/run", {"product": pid, "parts": [part], "total": q["total"], "ask": "x"}).get_json()
check(r.get("ok") and not r["made"] and r["notes"] and "can't say" in r["notes"][0] and db.balance(ws_of("a@example.com")) == bal2,
      "a line the claim rules refuse: nothing is charged and the chat says why")

print("== ready-made jobs on the desk")
llm.structured = fake_structured
d = page(a, "/app/desk")
check(d.count('class="dk-job"') == len(desk.PRESETS) and 'data-job-ask="A funny story ad for Citrus Volt' in d
      and all(os.path.exists(os.path.join(ROOT, "static", "agent", f"{i}.webp")) for i, *_ in desk.PRESETS),
      "the ready-made jobs, each with its own picture and a sentence written for the product")
check(all(re.search(desk.WANTS["story"], ask.lower()) for i, _t, _l, ask in desk.PRESETS if "story" in i),
      "the story jobs ask for a story ad in words the agent knows")

print(f"\nall {passed} story checks passed")
shutil.rmtree(TMP, ignore_errors=True)

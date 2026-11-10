"""The keyframe-first video pipeline (m15, keyframe_video.py) on a throwaway database.

  * KEYFRAME_VIDEO off (the default): nothing about a talking video changes. The jobs Create queues, their params
    and prices and exactly what the worker sends the vendor are compared with a snapshot taken before the pipeline
    existed (tests/fixtures/keyframe_off_snapshot.json), and the eight recipes that need the pipeline stay hidden
  * on: "keyframe_video" is in presets.PIPELINES, all fifteen recipes are offered, and a recipe's video is planned
    (beats of 4 to 8 seconds that add up to the length sold, the script cut into one line a beat, cutaways within
    the seconds a 30% margin allows, the end card); the member's price is the same per-duration price
  * the worker: the keyframe first (the presenter's still and the product photo as references, the product placed
    as the recipe says), its check whatever IMAGE_QA says, one silent retry, then a full refund before any video
    is paid for; every beat sent at once with the keyframe as its first reference; a beat made again once, then
    the whole job refunded; a cutaway that can't be made replaced by the packshot
  * the stitch (real ffmpeg on tiny clips made here): the length is the beats plus the end card, there is sound and
    it is levelled, the cutaway covers exactly its window while the beat's sound carries on, a screen recipe shows
    the member's screenshot with the presenter small in the corner, captions and tags are drawn, the end card is
    the real packshot on the brand colour
  * the frame check (VIDEO_QA): three frames a beat, a failing beat made again once, its own daily budget
  * a free fix re-renders the one beat the member points at, from the kept keyframe
  * the library's keyframe_tests kind: a cheap paid test that runs with the switch off, priced first
  * costs: every recipe's vendor cost against its price (keyframe_video.cost_table) and the README's table

No network: every vendor is faked, and a socket that tries to leave the machine fails the test. The stitching checks
need ffmpeg and ffprobe; without them they are skipped and the suite says so.

    python tests/test_keyframe_video.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_kf_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("FIRST_RUN_NUDGES", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "RS_FREE_PREVIEW", "RS_TALKING", "RS_SERVICE_PATH",
          "CLAIMS_CHECK", "IMAGE_QA", "IMAGE_QA_DAILY_MAX", "IMAGE_QA_MODEL", "FREE_FIX", "FREE_FIX_MONTHLY_CAP",
          "FREE_FIX_VIDEO_MONTHLY_CAP", "TEXT_LAYERS", "RS_MARKUP", "RS_HD_MARKUP", "RS_TALKING_DURATIONS",
          "PRESENTER_STILLS", "PRESENTER_VOICE_REF", "RS_LIBRARY_STILL_MODEL", "RS_LIBRARY_STILL_USD",
          "RS_VIDEO_USD_PER_SECOND", "RS_TALKING_USD_PER_SECOND", "RS_VIDEO_USD_PER_SECOND_480P",
          "RS_VIDEO_USD_PER_SECOND_1080P", "RS_CARD_REQUIRED", "UI_V2", "RS_JOB_CANCEL", "KEYFRAME_VIDEO", "VIDEO_QA",
          "VIDEO_QA_DAILY_MAX", "ANTHROPIC_FAST_MODEL", "LLM_USD_IN_PER_MTOK", "LLM_USD_OUT_PER_MTOK", "GROQ_API_KEY"):
    os.environ.pop(k, None)
# The keys are made-up strings: nothing here may reach a vendor with them (see the socket guard below).
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_PUBLIC_URL="https://campaignify.test", ANTHROPIC_API_KEY="sk-ant-test", RS_DISK_MIN_GB="0.2",
                  ADS_READY_EMAIL="0", MCP_SERVER="on")

leaks = []
_real_connect = socket.socket.connect


def _guard_connect(self, address, *a, **k):
    host = address[0] if isinstance(address, tuple) else str(address)
    if isinstance(address, tuple) and host not in ("127.0.0.1", "::1", "localhost"):
        leaks.append(address)
        raise AssertionError(f"a test tried to reach the network: {address}")
    return _real_connect(self, address, *a, **k)


socket.socket.connect = _guard_connect

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import free_fix  # noqa: E402
import hf  # noqa: E402
import image_qa  # noqa: E402
import keyframe_video as kfv  # noqa: E402
import llm  # noqa: E402
import presenters  # noqa: E402
import presets  # noqa: E402
import products  # noqa: E402
import stitch  # noqa: E402
import textlayer  # noqa: E402
import videotext  # noqa: E402
import worker  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import keyframe_off_snapshot  # noqa: E402

passed = 0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
FFMPEG = stitch.have() and os.environ.get("RS_TEST_NO_FFMPEG") != "1"      # the variable: run as a machine without it
if not stitch.have():
    stitch.have = lambda: True          # a machine without ffmpeg: everything before the stitch is still tested


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
    c.environ_base["REMOTE_ADDR"] = f"10.34.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.environ_base["HTTP_USER_AGENT"] = UA
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email, credits=0, admin=False, plan=None):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE users SET email_verified_at=?, is_admin=? WHERE id=?", (db.now(), 1 if admin else 0, u["id"]))
        if credits:
            conn.execute("UPDATE workspaces SET credits=? WHERE id=?", (credits, u["workspace_id"]))
        if plan:
            conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, u["workspace_id"]))
        conn.commit()
    return c, u


def post(c, url, body):
    return c.post(url, json=body, headers={"X-CSRF": csrf(c), "Accept": "application/json"})


def ws_row(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def job_row(jid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def params_of(jid):
    return json.loads(job_row(jid)["params_json"])


def outputs_of(jid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM outputs WHERE job_id=? ORDER BY id", (jid,)).fetchall()


def parts_of(jid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM video_parts WHERE job_id=? ORDER BY id", (jid,)).fetchall()


def qa_rows(jid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM asset_qa WHERE job_id=? ORDER BY id", (jid,)).fetchall()


def make_key(c, scope="read"):
    r = c.post("/app/mcp", data={"csrf": csrf(c), "action": "create", "name": f"test {scope}", "scope": scope})
    m = re.search(r"(cfy_[A-Za-z0-9_-]{40,})", r.get_data(as_text=True))
    return m.group(1) if m else None


anon = appmod.app.test_client()
N = [0]


def call(key, name, args=None):
    N[0] += 1
    r = anon.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "id": N[0],
                                "params": {"name": name, "arguments": args or {}}},
                  headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {key}"})
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json()["result"]


def png(size=(360, 640), color=(180, 170, 150)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


# ---------------------------------------------------------------- the fake vendors
COLORS = {"beat": [(200, 30, 30), (30, 160, 60), (150, 40, 170), (200, 120, 20)], "broll": (20, 60, 220)}
_clips = {}


def clip(color, seconds, quiet=False, size=(180, 320)):
    """A tiny real clip: one colour, a tone as its sound (quiet ones 26 dB lower, to see the levelling)."""
    key = (color, seconds, quiet, size)
    if key not in _clips:
        if not FFMPEG:
            _clips[key] = b"\x00\x00\x00\x18ftypmp42" + b"0" * 2000
        else:
            path = os.path.join(TMP, f"clip-{len(_clips)}.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            f"color=c=0x{color[0]:02x}{color[1]:02x}{color[2]:02x}:s={size[0]}x{size[1]}:r=24:d={seconds}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                            "-af", f"volume={'0.05' if quiet else '1.0'}", "-shortest", "-pix_fmt", "yuv420p", path],
                           check=True)
            _clips[key] = open(path, "rb").read()
    return _clips[key]


class Vendor:
    """The generation vendor, in memory. An image model answers with a picture; a video model with a real clip of
    the length asked for, each beat its own colour and a cutaway blue. `plan` holds what chosen requests answer
    instead ({"status": "failed"} ...), keyed by request number; `hold` keeps everything "in progress"."""

    def __init__(self):
        self.submits, self.uploads, self.n, self.files, self.plan, self.hold, self.canceled = [], [], 0, {}, {}, False, []

    def upload(self, data, ctype):
        self.uploads.append({"bytes": len(data), "type": ctype})
        return f"https://files.example/up{len(self.uploads)}"

    def submit(self, model, inputs, webhook_url=None, idempotency_key=None):
        self.n += 1
        self.submits.append({"model": model, "inputs": inputs, "key": idempotency_key, "n": self.n})
        return f"req-{self.n}"

    def cancel(self, rid):
        self.canceled.append(rid)
        return hf.CANCELED

    def status(self, rid):
        n = int(rid.split("-")[1])
        if self.hold:
            return {"status": "in_progress"}
        if n in self.plan:
            return self.plan[n]
        sub = self.submits[n - 1]
        if "image-to-video" in sub["model"]:
            url = f"https://cdn.example/{rid}.mp4"
            self.files[url] = clip(COLORS["broll"], int(sub["inputs"]["duration"]))
            return {"status": "completed", "video": {"url": url}}
        if "video" in sub["model"]:
            nth = sum(1 for s in self.submits[:n] if "reference-to-video" in s["model"])
            url = f"https://cdn.example/{rid}.mp4"
            self.files[url] = clip(COLORS["beat"][(nth - 1) % 4], int(sub["inputs"]["duration"]), quiet=nth % 2 == 0)
            return {"status": "completed", "video": {"url": url}}
        url = f"https://cdn.example/{rid}.png"
        self.files[url] = png()
        return {"status": "completed", "images": [{"url": url}]}

    def since(self, n):
        return self.submits[n:]


V = Vendor()
hf.upload, hf.submit, hf.status, hf.cancel = V.upload, V.submit, V.status, V.cancel
estimates = {"value": None}
hf.estimate = lambda model, inputs: estimates["value"]


class _Got:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def _fake_get(url, timeout=0, **kw):
    if url not in V.files:
        raise AssertionError(f"unexpected download {url}")
    return _Got(V.files[url])


worker.requests.get = _fake_get
if not FFMPEG:
    worker.make_poster = lambda path: None
model_calls, answers = [], []
GOOD = {"product_present": True, "photo_has_label": True, "label_matches": True, "label_legible": True, "other_text": [],
        "garbled_text": False, "people": True, "deformed_hands": False, "deformed_faces": False, "one_clear_face": True,
        "same_person": True, "notes": ""}


def _fake_anthropic(body, timeout=None):
    model_calls.append(body)
    ans = answers.pop(0) if answers else dict(GOOD)       # a clean answer unless a test queued another
    return {"model": body["model"], "usage": {"input_tokens": 2600, "output_tokens": 250},
            "content": [{"type": "tool_use", "name": body["tools"][0]["name"], "input": ans}]}


llm._anthropic = _fake_anthropic
STATIC = os.path.join(TMP, "static")
os.makedirs(os.path.join(STATIC, "presenters"))
os.makedirs(os.path.join(STATIC, "presets"))
presenters.STATIC_DIR = os.path.join(STATIC, "presenters")
presets.EXAMPLE_DIR = os.path.join(STATIC, "presets")
worker.STATIC = STATIC
presenters.HERE = TMP


def ticks(n=1):
    for _ in range(n):
        worker.tick()


def run(jid, limit=14):
    """Pass after pass until the job has ended."""
    for _ in range(limit):
        worker.tick()
        if job_row(jid)["status"] in ("done", "failed"):
            break
    return job_row(jid)


def frame_px(path, t, xy):
    out = os.path.join(TMP, "px.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", out], check=True)
    with Image.open(out) as im:
        return im.convert("RGB").getpixel(xy)


def near(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


SCRIPT20 = ("Still losing the lid of your travel mug? The Trail Mug has a lid that presses in and stays put. It is "
            "double-wall steel and holds twelve ounces. Find it at Northwind today.")
try:
    # ------------------------------------------------------------------ 1. off: exactly what it was
    print("== KEYFRAME_VIDEO off: a talking video is exactly what it was")
    snap = json.load(open(os.path.join(HERE, "fixtures", "keyframe_off_snapshot.json"), encoding="utf-8"))
    now = json.loads(json.dumps(keyframe_off_snapshot.collect(appmod)))
    hf.upload = V.upload
    for name in snap["configs"]:
        if now["configs"].get(name) != snap["configs"][name]:
            a, b = snap["configs"][name], now["configs"].get(name) or {}
            diff = [k for k in a if a[k] != b.get(k)]
            check(False, f"config {name!r} changed with the switch off: {diff}")
    check(len(snap["configs"]) == 12 and sum(len(c["jobs"]) for c in snap["configs"].values()) == 15
          and all("pipeline" not in j["params"] and "kf" not in j["params"] for c in now["configs"].values() for j in c["jobs"]),
          "12 configs (presenter, voiceover, seven one-take recipes, shapes, 1080p, a situation): the same jobs, "
          "params, prices and vendor inputs as before the pipeline existed")
    check(now["hidden"] == snap["hidden"] and now["recipes_for"] == snap["recipes_for"] and len(now["recipes_for"]) == 7
          and now["offered_recipes"] == snap["offered_recipes"] and now["pipelines"] == ["single_take"]
          and len(now["hidden"]) == 8 and all(v[1] == 0 and v[3] == 0 for v in now["hidden"].values()),
          "the eight recipes that need the pipeline are still hidden and refused; PIPELINES is one take only")
    check(not kfv.on() and "keyframe_video" not in presets.PIPELINES and presets.PIPELINES == ("single_take",)
          and len(presets.PIPELINES) == 1 and presets.pipeline_for(presets.get("talk-to-camera")) == "single_take"
          and not presets.runnable(presets.get("problem-solution")) and not kfv.video_qa_on(),
          "defaults: KEYFRAME_VIDEO off, VIDEO_QA off")
    with db.connect() as conn:
        check(conn.execute("SELECT COUNT(*) FROM video_parts").fetchone()[0] == 0, "and no job has parts")

    # ------------------------------------------------------------------ people and products for the rest
    adm, au = signup("owner@example.com", credits=200000, admin=True, plan="scale")
    mem, mu = signup("member@example.com", credits=60000)
    AWS, MWS = au["workspace_id"], mu["workspace_id"]
    photo_path = os.path.join(TMP, "product.png")
    shot = Image.new("RGB", (600, 750), (250, 250, 250))
    ImageDraw.Draw(shot).rectangle((180, 200, 420, 650), fill=(40, 90, 70))
    shot.save(photo_path)
    page_path = os.path.join(TMP, "page.png")
    Image.new("RGB", (600, 2400), (250, 220, 40)).save(page_path)

    def add_product(user, ws_id, brand_name, name, category=""):
        with appmod.app.test_request_context("/"):
            from flask import g
            with db.connect() as conn:
                g.user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
                pid = products.save_kit(conn, g.ws, brand_name=brand_name, product_name=name, audience="Hikers",
                                        claims="Keeps drinks hot for 6 hours")
                ph = products.add_photo(conn, ws_id, pid, photo_path, "product.png", "image/png")
                conn.execute("UPDATE products SET price='$24', offer='Free lid with every mug', category=? WHERE id=?",
                             (category or "home", pid))
                conn.execute("UPDATE brands SET kit_primary='#1F4D3A', kit_accent='#F5B301' WHERE id=(SELECT brand_id "
                             "FROM products WHERE id=?)", (pid,))
                conn.commit()
        return pid, ph
    PID, PH = add_product(mu, MWS, "Northwind", "Trail Mug")
    with db.connect() as conn:
        PAGE = products.add_photo(conn, MWS, PID, page_path, "page.png", "image/png")
        conn.execute("UPDATE products SET primary_photo_id=? WHERE id=?", (PH, PID))
        conn.commit()

    def queue(cfg, c=mem):
        cfg = dict({"product": PID, "photos": [PH]}, **cfg)
        q = post(c, "/app/create/quote", cfg).get_json()
        assert not q.get("error"), q
        r = post(c, "/app/create/generate", dict(cfg, total=q["total"])).get_json()
        with db.connect() as conn:
            return [j["id"] for j in conn.execute("SELECT id FROM jobs WHERE batch_id=? ORDER BY id", (r["batch"],))], q

    # ------------------------------------------------------------------ 2. on: the plan
    print("== KEYFRAME_VIDEO on: fifteen recipes, each planned in beats")
    os.environ["KEYFRAME_VIDEO"] = "1"
    check(kfv.on() and "keyframe_video" in presets.PIPELINES and list(presets.PIPELINES) == ["single_take", "keyframe_video"]
          and len(presets.recipes_for()) == 15 and all(presets.runnable(r) for r in presets.RECIPES)
          and all(presets.pipeline_for(r) == "keyframe_video" for r in presets.RECIPES)
          and presets.pipeline_for(presets.get("ugc")) == "single_take" and presets.pipeline_for(None) == "single_take",
          "the switch puts keyframe_video in PIPELINES: all 15 recipes are offered and take that path; a plain "
          "presenter video or voiceover (no recipe) stays one take")
    for rec in presets.RECIPES:
        own = rec.price["duration"]
        for dur in catalog.TOOLS["ugc_video"]["durations"]:
            beats = kfv.fit_beats(rec, dur)
            if sum(b["seconds"] for b in beats) != dur or not all(kfv.BEAT_MIN <= b["seconds"] <= kfv.BEAT_MAX for b in beats):
                check(False, f"{rec.id} at {dur} s: beats {[b['seconds'] for b in beats]}")
        if [b["seconds"] for b in kfv.fit_beats(rec, own)] != [b["seconds"] for b in rec.beats if b["beat"] != "end card"]:
            check(False, f"{rec.id}: its own length gives its own beats")
    check([b["seconds"] for b in kfv.fit_beats(presets.get("problem-solution"), 30)] == [5, 4, 6, 5, 5, 5]
          and [b["seconds"] for b in kfv.fit_beats(presets.get("problem-solution"), 8)] == [8]
          and [b["seconds"] for b in kfv.fit_beats(presets.get("talk-to-camera"), 30)] == [8, 7, 8, 7]
          and [b["seconds"] for b in kfv.fit_beats(presets.get("problem-solution"), 0, 4)] == [4, 4, 4],
          "every recipe at every length sold: whole beats of 4 to 8 seconds that add up to it (a longer video gets "
          "more beats, a shorter one fewer); the library's test makes each beat 4 seconds")
    lines = kfv.split_script(SCRIPT20, [6, 7, 7])
    check(len(lines) == 3 and " ".join(lines) == SCRIPT20 and lines[0].endswith("mug?") and all(lines)
          and all(len(x.split()) <= catalog.hard_words(s) for x, s in zip(lines, [6, 7, 7])),
          "the script is cut into one line a beat, in order, on sentence ends, nothing lost and nothing too long to say")
    two = kfv.split_script("Meet the mug. It keeps drinks hot.", [6, 7, 7])
    one = kfv.split_script("Hot, steel, yours.", [6, 7, 7])
    long_ = kfv.split_script("This mug keeps a drink hot for six hours and it fits in a bag and its lid presses in and "
                             "stays and it is made of steel", [4, 4])
    check(" ".join(two) == "Meet the mug. It keeps drinks hot." and all(two) and all(one) and len(one) == 3
          and all(long_) and len(long_) == 2 and kfv.split_script("", [6, 7]) == ["", ""]
          and kfv.split_script("One line.", [8]) == ["One line."],
          "fewer sentences than beats: cut at a comma, then between words, so no beat is silent")
    plans = {}
    for rec in presets.RECIPES:
        pl = kfv.plan(rec, {"duration": rec.price["duration"]}, "word " * catalog.max_words(rec.price["duration"]),
                      n_photos=1, has_still=True)
        plans[rec.id] = pl
        ok = (pl["seconds"] == rec.price["duration"] and len(pl["beats"]) == len([b for b in rec.beats if b["beat"] != "end card"])
              and all(b["line"] for b in pl["beats"]) and (pl["end_card"] is not None) == (rec.beats[-1]["beat"] == "end card")
              and pl["keyframe"]["kind"] == ("presenter" if rec.ad_type == "ugc" else "scene")
              and all(c["start"] + c["seconds"] <= pl["beats"][c["beat"]]["seconds"] - 0.39 for c in pl["cutaways"])
              and (not pl["cutaways"] or rec.ad_type == "ugc"))
        if not ok:
            check(False, f"plan of {rec.id}: {pl}")
    thc, ps = plans["talking-head-cutaways"], plans["problem-solution"]
    check([c["kind"] for c in thc["cutaways"]] == ["video"] and thc["cutaways"][0]["beat"] == 1
          and thc["cutaways"][0] == {"beat": 1, "kind": "video", "start": 0.8, "seconds": 5, "shot": "close shots of the product and its label"}
          and [c["kind"] for c in ps["cutaways"]] == ["video", "still"] and [c["beat"] for c in ps["cutaways"]] == [1, 2]
          and ps["cutaways"][1]["seconds"] == 5 and ps["cutaways"][1]["start"] + ps["cutaways"][1]["seconds"] <= 7 - kfv.BROLL_TAIL
          and all(not plans[r.id]["cutaways"] for r in presets.RECIPES if r.ad_type == "voiceover"),
          "a presenter recipe's product beats are spoken by the presenter with the product laid over them: one "
          "generated cutaway where the seconds allow it (5 of them in a 20-second video), the packshot itself after that; "
          "a voiceover's beats are product shots already")
    check(plans["street-interview"]["labels"] == ["AI presenter"] and plans["street-interview"]["captions"]
          and [b["tag"] for b in plans["how-to-steps"]["beats"]] == ["Step 1", "Step 2", "Step 3"]
          and [b["tag"] for b in plans["three-reasons-video"]["beats"]] == ["1", "2", "3"]
          and plans["offer-promo"]["end_card"]["badge"] and not plans["season-gift-clip"]["end_card"]["badge"]
          and plans["talk-to-camera"]["end_card"] is None and plans["founder-story"]["crossfade"] == 5
          and plans["talk-to-camera"]["crossfade"] == 0 and not plans["talk-to-camera"]["captions"]
          and [b["screen"] for b in plans["green-screen-product-page"]["beats"]] == [True, True]
          and [b["screen"] for b in plans["show-your-app"]["beats"]] == [False, True]
          and plans["hands-only-demo"]["keyframe"]["hands"] and not plans["vo-product-shots"]["keyframe"]["hands"]
          and kfv.plan(presets.get("talk-to-camera"), {"duration": 8, "placement": "holding"}, "Hi.")["keyframe"]["product_at"] == "hand"
          and kfv.plan(presets.get("talk-to-camera"), {"duration": 8, "captions": True}, "Hi.")["captions"] is True
          and kfv.plan(presets.get("how-to-steps"), {"duration": 20, "captions": False}, "a b c. d e f. g h i.")["captions"] is False,
          "the plan also holds what is set as type (captions by recipe or by the member, step tags, the AI presenter "
          "label), the end card (a badge only where the recipe shows the saved offer), a crossfade where the recipe "
          "asks, which beats show the member's screens, and where the product is in the keyframe")
    hd = kfv.plan(presets.get("problem-solution"), {"duration": 20, "resolution": "1080p"}, SCRIPT20, n_photos=1, has_still=True)
    short = kfv.plan(presets.get("problem-solution"), {"duration": 10}, SCRIPT20, n_photos=1, has_still=True)
    check([c["kind"] for c in hd["cutaways"]] == ["still", "still"] and kfv.broll_resolution("1080p") == "720p"
          and kfv.broll_resolution("480p") == "480p" and all(c["kind"] == "still" for c in short["cutaways"])
          and round(kfv._extra_seconds(20, "720p"), 1) == 7.4 and kfv._extra_seconds(20, "1080p") == 0,
          "the cap on generated seconds: about 0.4 x the seconds sold at 720p, none at 1080p (its markup is 1.4), "
          "so those cutaways are the packshot")

    # ------------------------------------------------------------------ 3. costs against prices
    print("== what each recipe costs us against what a member pays")
    table = {r["id"]: r for r in kfv.cost_table()}
    for rec in presets.RECIPES:
        r = table[rec.id]
        if r["credits"] != catalog.price_credits("ugc_video", duration=rec.price["duration"]) or r["credits"] != rec.price_credits():
            check(False, f"{rec.id}: the member's price changed")
    check(len(table) == 15 and {r["credits"] for r in table.values()} == {740, 1387, 1849},
          "member prices are the per-duration prices they were: 740, 1,387 and 1,849 credits")
    c8, c15, c20, cut = table["talk-to-camera"], table["street-interview"], table["how-to-steps"], table["talking-head-cutaways"]
    check(abs(c8["usd"] - (8 * 0.4622 + 0.23 + kfv.check_usd() * (1 + 3 * 1.4))) < 0.011
          and abs(c20["usd"] - (20 * 0.4622 + 0.215 + kfv.check_usd() * (1 + 9 * 1.4))) < 0.011
          and abs(cut["usd"] - (25 * 0.4622 + 0.23 + kfv.check_usd() * (1 + 9 * 1.4))) < 0.011
          and cut["cutaway_seconds"] == 5 and cut["vendor_seconds"] == 25 and table["problem-solution"]["stills"] == 1,
          "vendor cost = beat seconds and generated cutaway seconds at $0.4622, the keyframe ($0.20 + $0.015 a "
          "reference) and the checks")
    check(all(r["margin"] >= 0.30 and not any(f in ("negative", "under 30%") for f in r["flags"]) for r in table.values())
          and 0.45 < c8["margin"] < 0.48 and 0.47 < c15["margin"] < 0.49 and 0.35 < cut["margin"] < 0.37
          and all(r["vendor_seconds"] <= 1.4 * r["seconds"] for r in table.values()),
          "as planned, every recipe keeps at least 30%: 46 to 49% without a cutaway, 36% with one")
    check(c8["flags"] == ["negative if a beat is made twice"] and -0.08 < c8["worst_margin"] < 0
          and all(r["flags"] == ["under 30% if a beat is made twice"] for r in table.values() if r["seconds"] > 8)
          and 0.19 < c15["worst_margin"] < 0.21 and 0.29 < c20["worst_margin"] < 0.30 and 0.16 < cut["worst_margin"] < 0.18
          and all(r["seconds_cap"] == int(2.1 * r["seconds"]) and r["beat_again"] for r in table.values())
          and kfv.hard_seconds(8) == 8 * 2.1 and round(kfv.hard_seconds(20, "1080p"), 1) == 29.4,
          "flagged: when the frame check sends a beat back (and the keyframe was made twice), an 8-second recipe costs "
          "6% more than it sold for, a 15-second one keeps 20%, a 20-second one just under 30%, one with a cutaway 17%. "
          "No job's video seconds may pass 2.1 x the seconds sold")
    hd_table = {r["id"]: r for r in kfv.cost_table("1080p")}
    check(all("under 30%" in r["flags"] and 0.26 < r["margin"] < 0.28 for r in hd_table.values())
          and all(r["cutaway_seconds"] == 0 for r in hd_table.values())
          and all(r["beat_again"] == (r["seconds"] == 20) for r in hd_table.values())
          and all(r["worst_margin"] > 0 for r in hd_table.values()),
          "flagged: at 1080p every recipe is at 27% as planned (the 1080p markup is 1.4, as for one take today); its "
          "cutaways are the packshot, and only a 20-second video has the seconds to make a beat again")
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    sec = readme.split("Keyframe-first video")[1] if "Keyframe-first video" in readme else ""
    for r in table.values():
        row = next((ln for ln in sec.splitlines() if ln.startswith(f"| {r['name']} ")), "")
        if f"${r['usd']:.2f}" not in row or f"{r['credits']:,}" not in row or f"{round(r['margin'] * 100)}%" not in row:
            check(False, f"README's cost table has {r['name']}: ${r['usd']:.2f}, {r['credits']:,} credits, {round(r['margin'] * 100)}% ({row!r})")
    check("KEYFRAME_VIDEO" in sec and "VIDEO_QA" in sec and "keyframe_tests" in sec,
          "the README's table holds every recipe's vendor cost, price and margin as cost_table() gives them")

    # ------------------------------------------------------------------ 4. Create and the price
    print("== Create with the switch on: the same price, a planned job")
    boot = json.loads(mem.get(f"/app/create?product={PID}&template=problem-solution").get_data(as_text=True).split(
        '<script id="cf-boot" type="application/json">')[1].split("</script>")[0])
    check(boot["prefill"].get("preset") == "problem-solution" and boot["prefill"]["duration"] == 20,
          "a recipe that needed the pipeline can be started from its link now")
    cfg = {"type": "ugc", "preset": "problem-solution", "items": [SCRIPT20], "presenters": ["maya"]}
    V.hold = True
    (J1,), q1 = queue(cfg)
    p1, j1 = params_of(J1), job_row(J1)
    check(q1["total"] == 1849 == j1["credits"] and p1["pipeline"] == "keyframe_video" and p1["preset"] == "problem-solution"
          and p1["kf"]["seconds"] == 20 and [b["line"] for b in p1["kf"]["beats"]] == kfv.split_script(SCRIPT20, [6, 7, 7])
          and abs(j1["cost_usd"] - kfv.plan_cost(p1["kf"])["usd"]) < 1e-4 and j1["cost_usd"] > 11.5
          and p1["kf"]["keyframe"]["refs"] == 1 and j1["template_id"] == "problem-solution",
          "problem, then solution: 1,849 credits as always; the job carries its plan, and its cost to us is the "
          "plan's (the keyframe and the cutaway included)")
    plain = {"type": "ugc", "items": ["Meet the mug."], "presenters": ["maya"], "duration": 8}
    (JP,), qp = queue(plain)
    check(qp["total"] == 740 and "pipeline" not in params_of(JP) and not worker._keyframe_job(job_row(JP)),
          "a presenter video with no recipe is still one take")
    with db.connect() as conn:
        worker._fail(conn, job_row(JP), worker.CANCELED)
    for bad, why in (({"type": "ugc", "preset": "green-screen-product-page", "items": ["This is the page."], "presenters": ["maya"]},
                      "a screenshot of your product page"),
                     ({"type": "ugc", "preset": "show-your-app", "items": ["This is the app."], "presenters": ["maya"]},
                      "your app's screenshots")):
        r = post(mem, "/app/create/quote", dict(bad, product=PID)).get_json()
        check(r["count"] == 0 and why in r["error"] and "nothing of it is ever generated" in r["error"] and r["step"] == "format",
              f"a recipe that shows the member's own screens says what it needs: {why}")
    r = post(mem, "/app/create/quote", {"type": "ugc", "preset": "green-screen-product-page", "items": ["This is the page."],
                                        "presenters": ["maya"], "product": PID, "screens": [PAGE]}).get_json()
    r2 = post(mem, "/app/create/quote", {"type": "ugc", "preset": "green-screen-product-page", "items": ["This is the page."],
                                         "presenters": ["maya"], "product": PID, "screens": [999999]}).get_json()
    check(r["count"] == 1 and r["total"] == 1387 and r2["count"] == 0,
          "with a screenshot of theirs (a photo id of the product, `screens`) it is priced; another's id is ignored")
    r = post(mem, "/app/create/quote", dict(cfg, product=PID, cta="Guaranteed to cure anxiety")).get_json()
    check(r["count"] == 0 and "Button text" in r["error"], "the end card's button text goes through the rules")

    # ------------------------------------------------------------------ 5. the worker: keyframe, check, beats
    print("== the worker: the keyframe first, its check, then every beat at once")
    bal_start = db.balance(ws_row(MWS))
    n0, c0, u0 = len(V.submits), len(model_calls), len(V.uploads)
    ticks()
    subs = V.since(n0)
    kf_in = subs[0]["inputs"]
    check(len(subs) == 1 and subs[0]["model"] == "marketing-studio/image/sunburst" and job_row(J1)["status"] == "running"
          and job_row(J1)["request_id"] is None and len(kf_in["image_urls"]) == 1 and kf_in["quality"] == "high"
          and kf_in["resolution"] == "1k" and kf_in["aspect_ratio"] == "9:16" and kf_in["enhance_prompt"] is False,
          "first pass: only the keyframe is sent, to the library's image model, with the product photo as its reference")
    maya = presenters.record("maya")
    check(maya["appearance"] in kf_in["prompt"] and "It stands upright on the kitchen counter beside them" in kf_in["prompt"]
          and "Their hands stay off it" in kf_in["prompt"] and "the Trail Mug from the reference photos" in kf_in["prompt"]
          and "The only readable text anywhere is what is printed on the product itself" in kf_in["prompt"]
          and presenters.FICTIONAL in kf_in["prompt"] and "Image 1 shows a person" not in kf_in["prompt"],
          "with no approved still the person is drawn from the roster's description, in their place, the product "
          "on the surface beside them with its label to the camera")
    ticks()
    check(len(V.since(n0)) == 1 and [p["status"] for p in parts_of(J1)] == ["running"] and len(model_calls) == c0,
          "while the keyframe is being made nothing else is sent")
    V.hold = False
    ticks()          # the keyframe arrives, is checked, and every beat and the cutaway go out together
    V.hold = True
    parts = parts_of(J1)
    check(parts[0]["status"] == "done" and os.path.exists(parts[0]["path"]) and len(model_calls) == c0 + 1
          and not image_qa.on() and qa_rows(J1)[0]["kind"] == "keyframe" and qa_rows(J1)[0]["verdict"] == "pass"
          and "presenter keyframe" in json.dumps(model_calls[-1]["messages"]),
          "the keyframe is kept on the job and checked in keyframe mode, with IMAGE_QA off")
    subs = V.since(n0)[1:]
    beats = [s for s in subs if "reference-to-video" in s["model"]]
    broll = [s for s in subs if "image-to-video" in s["model"]]
    check(len(beats) == 3 and len(broll) == 1 and [b["inputs"]["duration"] for b in beats] == [6, 7, 7]
          and len({s["key"] for s in subs}) == 4 and len(V.uploads) == u0 + 2
          and all(b["inputs"]["image_urls"][0] == beats[0]["inputs"]["image_urls"][0] != kf_in["image_urls"][0] for b in beats)
          and all(b["inputs"]["image_urls"][1:] == kf_in["image_urls"] for b in beats)
          and all(b["inputs"]["resolution"] == "720p" and b["inputs"]["generate_audio"] is True
                  and b["inputs"]["aspect_ratio"] == "9:16" and "audio_urls" not in b["inputs"] for b in beats),
          "one pass sends all three beats and the cutaway: each beat is a reference-to-video request of its own "
          "length with the approved keyframe (uploaded once) as its first reference, then the product photo")
    lines = [b["line"] for b in p1["kf"]["beats"]]
    check(all(f'They say: "{ln}"' in b["inputs"]["prompt"] for ln, b in zip(lines, beats))
          and all("Image 1 is its first frame: the same person" in b["inputs"]["prompt"]
                  and catalog.UNPOLISHED in b["inputs"]["prompt"] and "never invent other branding" in b["inputs"]["prompt"]
                  and "no on-screen text, no subtitles, no music" in b["inputs"]["prompt"]
                  and "warm, friendly female voice" in b["inputs"]["prompt"] for b in beats)
          and broll[0]["inputs"]["duration"] == 5 and broll[0]["inputs"]["generate_audio"] is False
          and broll[0]["inputs"]["image_url"] == kf_in["image_urls"][0] and catalog.KEEP_PRODUCT in broll[0]["inputs"]["prompt"]
          and "Hands set the product down" in broll[0]["inputs"]["prompt"],
          "each beat says its own line of the script, with the realism and fidelity lines; the cutaway is a silent "
          "image-to-video clip from the real product photo")
    ticks(2)
    check(len(V.since(n0)) == 5 and job_row(J1)["status"] == "running" and outputs_of(J1) == []
          and [p["status"] for p in parts_of(J1)].count("running") == 4,
          "nothing is sent twice while the beats are being made")
    with db.connect() as conn:          # a crash between sending a part and writing its request id down
        lost = conn.execute("SELECT * FROM video_parts WHERE job_id=? AND kind='beat' AND idx=1", (J1,)).fetchone()
        conn.execute("UPDATE video_parts SET status='pending', request_id=NULL WHERE id=?", (lost["id"],))
        conn.commit()
    ticks()
    check(V.submits[-1]["key"] == lost["submit_key"] and V.submits[-1]["inputs"] == json.loads(lost["inputs_json"]),
          "a part that was sent but not written down is sent again with the same idempotency key (not billed twice)")
    V.hold = False
    if FFMPEG:
        j = run(J1)
        outs = outputs_of(J1)
        check(j["status"] == "done" and len(outs) == 1 and outs[0]["kind"] == "video" and outs[0]["url"] == ""
              and os.path.exists(outs[0]["path"]) and db.balance(ws_row(MWS)) == bal_start and j["credits"] == 1849
              and mem.get(f"/media/{outs[0]['id']}").status_code == 200 and os.path.exists(outs[0]["path"] + ".jpg"),
              "then it is stitched and delivered as one video, served from our disk, at the price quoted")
        FINAL = outs[0]["path"]
        info = stitch.probe(FINAL)
        check(abs(info["duration"] - 22.0) < 0.2 and info["audio"] and (info["width"], info["height"]) == (180, 320)
              and info["fps"] == 24.0 and abs(info["audio_seconds"] - 22.0) < 0.2,
              "its length is the three beats plus the 2-second end card (22 s), one size and rate, with sound throughout")
        loud = [stitch.loudness(FINAL, a, 5) for a in (0.5, 6.5, 13.5)]
        check(max(loud) - min(loud) < 3.0 and stitch.loudness(FINAL, 20.4, 1.4) < -60
              and stitch.loudness(os.path.join(TMP, "clip-1.mp4") if os.path.exists(os.path.join(TMP, "clip-1.mp4")) else FINAL) is not None,
              "the beats came in 26 dB apart and are levelled to within 3 dB; the end card's tail is quiet")
        # each beat's clip is its own colour; the second beat's is the fourth (it was sent again above)
        red, green, purple = COLORS["beat"][0], COLORS["beat"][3], COLORS["beat"][2]
        mid = (90, 120)
        check(near(frame_px(FINAL, 3.0, mid), red) and near(frame_px(FINAL, 6.3, mid), green)
              and near(frame_px(FINAL, 6.0 + 0.8 + 0.3, mid), COLORS["broll"]) and near(frame_px(FINAL, 6.0 + 0.8 + 4.7, mid), COLORS["broll"])
              and near(frame_px(FINAL, 6.0 + 5.8 + 0.3, mid), green) and near(frame_px(FINAL, 12.7, mid), green),
              "the cutaway covers the second beat's picture from 0.8 s for 5 s, exactly, and the presenter is back "
              "after it: the beat's own sound never stopped")
        check(stitch.loudness(FINAL, 7.5, 3) > -30, "(there is speech under the cutaway)")
        still_px = frame_px(FINAL, 13.0 + 0.8 + 2.5, mid)
        check(not near(still_px, purple, 25) and near(frame_px(FINAL, 13.3, mid), purple)
              and near(frame_px(FINAL, 13.0 + 5.8 + 0.6, mid), purple),
              "the third beat's cutaway is the member's packshot itself (a slow push-in), then the presenter closes")
        end_px = frame_px(FINAL, 21.2, (6, 6))
        column = [frame_px(FINAL, 21.2, (90, y)) for y in range(60, 300, 12)]
        check(near(end_px, (31, 77, 58), 20) and near(frame_px(FINAL, 21.2, (174, 314)), (31, 77, 58), 20)
              and any(near(c_, (250, 250, 250), 25) for c_ in column) and any(near(c_, (40, 90, 70), 20) for c_ in column),
              "the end card is the brand's colour with the real product photo set on it")
        cap_on, cap_off = frame_px(FINAL, 1.0, (90, 240)), frame_px(FINAL, 0.02, (90, 240))
        check(p1["kf"]["captions"] and not near(cap_on, red, 60) and near(cap_off, red, 40),
              "captions are burned in over the beat, low in the safe area (this recipe has them on)")
        left = [p for p in parts_of(J1)]
        check(all(p["status"] == "done" for p in left) and os.path.exists(left[0]["path"])
              and all(p["path"] is None for p in left if p["kind"] != "keyframe")
              and not os.path.exists(os.path.join(kfv.parts_dir(job_row(J1)), "work")),
              "afterwards the keyframe stays with the job; the beats are removed (FREE_FIX is off)")
        with db.connect() as conn:
            k = kfv.keyframe_of(conn, J1)
        check(k is not None and k["kind"] == "keyframe" and kfv.beats_of(job_row(J1))[1] == {
            "n": 2, "name": "product", "seconds": 7, "line": lines[1]}, "keyframe_of() and beats_of() read it back")
    else:
        print("  SKIPPED: ffmpeg isn't installed here, so nothing can be stitched (the checks of the finished video)")
        with db.connect() as conn:
            worker._fail(conn, job_row(J1), worker.CANCELED)

    # ------------------------------------------------------------------ 6. the keyframe check: retry, then refund
    print("== the keyframe check: one silent retry, then a full refund before any video")
    BAD = dict(GOOD, label_matches=False)
    bal, n0 = db.balance(ws_row(MWS)), len(V.submits)
    answers[:] = [BAD]
    (J2,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."],
                       "presenters": ["maya"]})
    cost0 = job_row(J2)["cost_usd"]
    ticks(2)
    kparts = [p for p in parts_of(J2) if p["kind"] == "keyframe"]
    check([(p["attempt"], p["status"]) for p in kparts] == [(1, "failed"), (2, "running")]
          and kparts[0]["error"].startswith("check: The label or logo doesn't match")
          and all("image" in s["model"] and "video" not in s["model"] for s in V.since(n0))
          and abs(job_row(J2)["cost_usd"] - cost0 - 0.215) < 1e-6 and job_row(J2)["status"] == "running"
          and job_row(J2)["error"] is None and db.balance(ws_row(MWS)) == bal - 740,
          "a keyframe that fails its check is made again once, silently: no video is sent, the member sees "
          "nothing, and the second keyframe is added to what the job cost us")
    if FFMPEG:
        j = run(J2)
        check(j["status"] == "done" and [r["verdict"] for r in qa_rows(J2)] == ["fail", "pass"]
              and abs(stitch.probe(outputs_of(J2)[0]["path"])["duration"] - 8.0) < 0.2,
              "the second keyframe passes and the video is made (one beat, no end card: 8 s)")
    else:
        with db.connect() as conn:
            worker._fail(conn, job_row(J2), worker.CANCELED)
    bal, n0 = db.balance(ws_row(MWS)), len(V.submits)
    answers[:] = [BAD, dict(GOOD, one_clear_face=False)]
    (J3,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."],
                       "presenters": ["maya"]})
    j = run(J3)
    check(j["status"] == "failed" and j["error"] == image_qa.FAIL_TEXT and db.balance(ws_row(MWS)) == bal
          and len(V.since(n0)) == 2 and all("sunburst" in s["model"] for s in V.since(n0)) and outputs_of(J3) == []
          and [r["verdict"] for r in qa_rows(J3)] == ["fail", "fail"]
          and "face isn't clear" in qa_rows(J3)[1]["reasons_json"]
          and not os.path.exists(kfv.parts_dir(job_row(J3))),
          "a second failure stops the job before any video is paid for: \"we couldn't get this one right\", every "
          "credit back, two image requests and not one video request")
    n0 = len(V.submits)
    V.plan[V.n + 1] = {"status": "nsfw", "error": "blocked"}
    V.plan[V.n + 2] = {"status": "failed"}
    (J4,), _q = queue({"type": "voiceover", "preset": "season-gift-clip", "items": ["A present they will actually use."]})
    j = run(J4)
    check(j["status"] == "failed" and j["error"] == image_qa.FAIL_TEXT and len(V.since(n0)) == 2
          and parts_of(J4)[0]["error"].startswith("content filter: blocked"),
          "a keyframe the vendor refuses or fails twice ends the same way")
    key = os.environ.pop("ANTHROPIC_API_KEY")
    n0, c0 = len(V.submits), len(model_calls)
    V.hold = True
    (J5,), _q = queue({"type": "voiceover", "preset": "season-gift-clip", "items": ["A present they will actually use."]})
    ticks()
    V.hold = False
    ticks(2)
    os.environ["ANTHROPIC_API_KEY"] = key
    vo_kf = V.since(n0)[0]["inputs"]["prompt"]
    check(qa_rows(J5)[0]["verdict"] == "skipped" and len(model_calls) == c0 and any("video" in s["model"] for s in V.since(n0))
          and "No people and no hands in frame." in vo_kf and "The shot: a slow drift towards the product among plain parcels" in vo_kf
          and "selfie" not in vo_kf,
          "when the check can't run (no vision key) the video goes ahead unchecked; a voiceover's keyframe is the "
          "product in its scene, with nobody in it")
    vo_beat = next(s for s in V.since(n0) if "reference-to-video" in s["model"])["inputs"]
    check('A narrator, never seen on camera, says in a deep, smooth narrator voice: "A present they will actually use."' in vo_beat["prompt"]
          and "Image 1 is its first frame: the same scene" in vo_beat["prompt"] and vo_beat["duration"] == 8,
          "and its beat is narrated over that scene")
    with db.connect() as conn:
        worker._fail(conn, job_row(J5), worker.CANCELED)
    estimates["value"] = 0.90
    bal, n0 = db.balance(ws_row(MWS)), len(V.submits)
    (J6,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one."], "presenters": ["maya"]})
    j = run(J6)
    estimates["value"] = None
    check(j["status"] == "failed" and "price changed" in j["error"] and len(V.since(n0)) == 0 and db.balance(ws_row(MWS)) == bal,
          "a keyframe whose live vendor estimate is far over our price is refused before it is sent, and refunded")
    bal, n0, had = db.balance(ws_row(MWS)), len(V.submits), stitch.have
    stitch.have = lambda: False
    (J6b,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one."], "presenters": ["maya"]})
    j = run(J6b)
    stitch.have = had
    check(j["status"] == "failed" and j["error"].startswith(kfv.NO_FFMPEG) and len(V.since(n0)) == 0 and not parts_of(J6b)
          and db.balance(ws_row(MWS)) == bal,
          "a server without ffmpeg refuses a keyframe-first video when it starts: nothing is sent, the credits come back")

    # ------------------------------------------------------------------ 7. a presenter's still and voice
    print("== the presenter's approved still and voice sample")
    os.makedirs(os.path.join(STATIC, "presenters", "maya"))
    open(os.path.join(STATIC, "presenters", "maya", "outdoor.webp"), "wb").write(png((270, 480)))
    open(os.path.join(STATIC, "presenters", "maya", "voice.mp3"), "wb").write(b"ID3" + b"0" * 900)
    os.environ["PRESENTER_VOICE_REF"] = "1"
    n0 = len(V.submits)
    V.hold = True
    (J7,), _q = queue({"type": "ugc", "preset": "street-interview", "items": ["What is the Trail Mug? A steel mug with a lid that "
                                                                              "stays. It is for anyone out early."],
                       "presenters": ["maya"], "placement": "holding"})
    ticks()
    kin = V.since(n0)[0]["inputs"]
    check(len(kin["image_urls"]) == 2 and kin["prompt"].startswith("Image 1 shows a person in a room. Make a new photo of "
                                                                   "exactly the same person in the same room")
          and "from the other reference images" in kin["prompt"] and "They hold it up beside their shoulder in one hand" in kin["prompt"]
          and maya["appearance"] not in kin["prompt"] and params_of(J7)["kf"]["keyframe"]["refs"] == 2
          and params_of(J7)["situation"] == "outdoor",
          "with an approved still of the presenter's situation it is the first reference and the person, room and "
          "clothes are kept from it; `placement: holding` puts the product in their hand")
    V.hold = False
    ticks()
    V.hold = True
    b7 = [s for s in V.since(n0) if "reference-to-video" in s["model"]]
    check(len(b7) == 2 and all(len(b["inputs"]["audio_urls"]) == 1 and "Their voice is the voice heard in audio 1" in b["inputs"]["prompt"]
                               and "They keep holding it as in image 1" in b["inputs"]["prompt"] for b in b7),
          "PRESENTER_VOICE_REF: each beat carries the presenter's voice sample as its audio reference")
    os.environ.pop("PRESENTER_VOICE_REF")
    shutil.rmtree(os.path.join(STATIC, "presenters", "maya"))
    V.hold = False
    with db.connect() as conn:
        worker._fail(conn, job_row(J7), worker.CANCELED)
    check(len(V.canceled) >= 2 and all(p["status"] == "failed" for p in parts_of(J7) if p["kind"] == "beat")
          and not os.path.exists(kfv.parts_dir(job_row(J7))),
          "a job that ends early asks the vendor to drop the beats still being made and keeps nothing")
    place = catalog.beat_prompt("ugc", "Trail Mug", "Find it at Northwind.", presenter_id="maya", voice_id="warm_f",
                                new_place="outdoor")
    check("the same person (their face, hair, age and build exactly as in that image) and the same product, now in a "
          "quiet residential street" in place and "standing on the top of the low wall beside them" in place,
          "a beat somewhere else is \"the same person, a new scene\" from the same keyframe")

    # ------------------------------------------------------------------ 8. a beat fails: once more, then refund
    print("== a beat that fails is made again once; after that the whole job is refunded")
    cfg3 = {"type": "voiceover", "preset": "how-to-steps", "items": ["First, fill it. Second, press the lid in until it "
                                                                     "sits flat. Third, take it with you today."]}
    bal, n0 = db.balance(ws_row(MWS)), len(V.submits)
    (J8,), _q = queue(cfg3)
    ticks()                                       # the keyframe is sent; the three beats will be the next requests
    V.plan[V.n + 2] = {"status": "failed", "error": "model overloaded"}      # the second beat fails once
    if FFMPEG:
        j = run(J8)
        bp = [p for p in parts_of(J8) if p["kind"] == "beat"]
        check(j["status"] == "done" and [(p["idx"], p["attempt"], p["status"]) for p in bp] == [
            (0, 1, "done"), (1, 1, "failed"), (2, 1, "done"), (1, 2, "done")] and bp[1]["error"] == "generation failed: model overloaded"
              and len([s for s in V.since(n0) if "video" in s["model"]]) == 4 and db.balance(ws_row(MWS)) == bal - 1849
              and abs(stitch.probe(outputs_of(J8)[0]["path"])["duration"] - 22.0) < 0.2,
              "one beat failed at the vendor: only that beat is made again, and the video is delivered whole")
        tag_px = [frame_px(outputs_of(J8)[0]["path"], 3.0, (x, y)) for x in (15, 18, 24, 30, 38) for y in (47, 50, 56)]
        check(any(near(c_, (245, 179, 1), 45) for c_ in tag_px)
              and not any(near(frame_px(outputs_of(J8)[0]["path"], 3.0, (150, y)), (245, 179, 1), 45) for y in (47, 50, 56)),
              "its step tag is set on the beat, top left of the safe area, in the brand's accent colour")
    else:
        with db.connect() as conn:
            worker._fail(conn, job_row(J8), worker.CANCELED)
    bal, n0, nc = db.balance(ws_row(MWS)), len(V.submits), len(V.canceled)
    (J9,), _q = queue(cfg3)
    ticks()                                       # the keyframe is sent
    V.plan[V.n + 3] = {"status": "failed"}        # the third beat will fail
    ticks(2)                                      # the beats go out; two arrive and the third fails
    V.plan[V.n + 1] = {"status": "failed"}        # ... and its second try fails too
    j = run(J9)
    check(j["status"] == "failed" and j["error"] == "The generation failed. Credits refunded." and db.balance(ws_row(MWS)) == bal
          and outputs_of(J9) == [] and not os.path.exists(kfv.parts_dir(job_row(J9)))
          and [p["attempt"] for p in parts_of(J9) if p["kind"] == "beat" and p["idx"] == 2] == [1, 2],
          "a beat that fails twice ends the whole job: never a partial video, every credit back, nothing kept")

    # ------------------------------------------------------------------ 9. the frame check (VIDEO_QA)
    print("== the frame check (VIDEO_QA)")
    if FFMPEG:
        os.environ["VIDEO_QA"] = "1"
        FACE = dict(GOOD, same_person=False)
        bal, n0, c0 = db.balance(ws_row(MWS)), len(V.submits), len(model_calls)
        # keyframe ok; beat 1's frames: fine, wrong face, wrong face -> made again -> three fine frames; beat 2: fine
        answers[:] = [GOOD, GOOD, FACE, FACE]
        (J10,), _q = queue({"type": "ugc", "preset": "street-interview", "presenters": ["maya"],
                            "items": ["What is the Trail Mug? A steel mug with a lid that stays. It is for anyone out early."]})
        cost0 = job_row(J10)["cost_usd"]
        j = run(J10)
        fr = [r for r in qa_rows(J10) if r["kind"] == "frame"]
        check(j["status"] == "done" and len(fr) == 9 and [r["verdict"] for r in fr[:3]] == ["pass", "fail", "fail"]
              and all(r["verdict"] == "pass" for r in fr[3:]) and sorted({r["idx"] for r in fr}) == [1, 2, 3, 11, 12, 13]
              and "The presenter's face changed from the keyframe." in fr[1]["reasons_json"]
              and len([s for s in V.since(n0) if "reference-to-video" in s["model"]]) == 3
              and abs(job_row(J10)["cost_usd"] - cost0 - 7 * 0.4622) < 1e-3 and db.balance(ws_row(MWS)) == bal - 1387
              and len(model_calls) == c0 + 10,
              "three frames of every beat are checked against the product photo and the keyframe's face; a beat with "
              "two bad frames is made again once (its seconds added to our cost) and the video is delivered")
        three = model_calls[-1]["messages"][0]["content"]
        check(len([x for x in three if x.get("type") == "image"]) == 3 and "Image 3 is a frame taken from the generated video" in json.dumps(three),
              "a frame check shows the model the product photo, the keyframe and the frame")
        bal = db.balance(ws_row(MWS))
        answers[:] = [GOOD, GOOD, FACE, GOOD]                  # one odd frame of three: the beat stands
        (J11,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."],
                            "presenters": ["maya"]})
        n0 = len(V.submits)
        j = run(J11)
        check(j["status"] == "done" and len([s for s in V.since(n0) if "reference-to-video" in s["model"]]) == 1,
              "one odd frame of three doesn't send a beat back")
        bal = db.balance(ws_row(MWS))
        answers[:] = [GOOD] + [dict(GOOD, label_matches=False)] * 6
        (J12,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."],
                            "presenters": ["maya"]})
        n0 = len(V.submits)
        j = run(J12)
        check(j["status"] == "failed" and j["error"] == image_qa.FAIL_TEXT and db.balance(ws_row(MWS)) == bal
              and len([s for s in V.since(n0) if "reference-to-video" in s["model"]]) == 2 and outputs_of(J12) == [],
              "a beat that fails the frame check twice: \"we couldn't get this one right\", credits returned")
        os.environ["VIDEO_QA_DAILY_MAX"] = "0"
        c0 = len(model_calls)
        (J13,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."],
                            "presenters": ["maya"]})
        j = run(J13)
        os.environ.pop("VIDEO_QA_DAILY_MAX")
        with db.connect() as conn:
            vids, pics = image_qa.checks_today(conn, image_qa.VIDEO_KINDS), image_qa.checks_today(conn)
        check(j["status"] == "done" and len(model_calls) == c0 and all(r["verdict"] == "skipped" for r in qa_rows(J13))
              and vids > 20 and pics == 0 and kfv.video_qa_daily_max() == 3000,
              "past VIDEO_QA_DAILY_MAX videos are delivered unchecked; a video's checks and a picture's are counted "
              "apart (neither budget eats the other's)")
        os.environ.pop("VIDEO_QA")
        c0 = len(model_calls)
        (J14,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."],
                            "presenters": ["maya"]})
        j = run(J14)
        check(j["status"] == "done" and len(model_calls) == c0 + 1 and not [r for r in qa_rows(J14) if r["kind"] == "frame"],
              "VIDEO_QA off (the default): only the keyframe is checked")
        # a 1080p re-render of it copies its params, plan and all: the plan is made again for 1080p
        V.hold = True
        n0 = len(V.submits)
        r = mem.post(f"/app/jobs/{J14}/hd", data={"csrf": csrf(mem), "how": "rerender", "total": str(
            catalog.price_credits("ugc_video", duration=8, resolution="1080p"))}, headers={"Accept": "application/json"})
        with db.connect() as conn:
            HD = conn.execute("SELECT id FROM jobs WHERE parent_job_id=? ORDER BY id DESC LIMIT 1", (J14,)).fetchone()["id"]
        ticks()
        V.hold = False
        ticks()
        hd_beats = [s_ for s_ in V.since(n0) if "reference-to-video" in s_["model"]]
        check(r.status_code == 200 and job_row(HD)["credits"] == 1274 and params_of(HD)["kf"]["resolution"] == "1080p"
              and len(hd_beats) == 1 and hd_beats[0]["inputs"]["resolution"] == "1080p",
              "the 1080p re-render of a keyframe-first video is planned again and rendered at 1080p")
        with db.connect() as conn:
            worker._fail(conn, job_row(HD), worker.CANCELED)
    else:
        print("  SKIPPED: the frame check cuts frames with ffmpeg")

    # ------------------------------------------------------------------ 10. the member's own screens
    print("== recipes that show the member's own screens")
    if FFMPEG:
        n0 = len(V.submits)
        (J15,), q15 = queue({"type": "ugc", "preset": "green-screen-product-page", "presenters": ["maya"], "screens": [PAGE],
                             "items": ["This is the page for the Trail Mug. It is twenty-four dollars, as the page says. "
                                       "The button is right there."]})
        j = run(J15)
        f15 = outputs_of(J15)[0]["path"]
        x, y, pw, ph = stitch.pip_box((180, 320), textlayer.SAFE["9:16"][3])
        check(j["status"] == "done" and params_of(J15)["screens"] == [PAGE] and q15["total"] == 1387
              and near(frame_px(f15, 3.0, (140, 60)), (250, 220, 40)) and near(frame_px(f15, 10.0, (140, 60)), (250, 220, 40))
              and near(frame_px(f15, 3.0, (x + pw // 2, y + ph // 2)), COLORS["beat"][(sum(1 for s in V.submits[:n0 + 2] if "reference-to-video" in s["model"]) - 1) % 4], 60)
              and not any("image-to-video" in s["model"] for s in V.since(n0))
              and abs(stitch.probe(f15)["duration"] - 17.0) < 0.2,
              "over the product page: the member's real screenshot fills the frame for both beats, the presenter is "
              "small in the corner above what a platform covers, and nothing of the page was generated")
        APID, APH = add_product(mu, MWS, "Northwind", "Northwind Planner", "app_service")
        (J16,), _q = queue({"type": "ugc", "preset": "show-your-app", "presenters": ["maya"], "product": APID, "photos": [APH],
                            "items": ["This is Northwind Planner. Tap a day and add a task. It is free to start."]})
        j = run(J16)
        f16 = outputs_of(J16)[0]["path"]
        check(j["status"] == "done" and near(frame_px(f16, 10.0, (90, 100)), (250, 250, 250), 30)
              and near(frame_px(f16, 10.0, (20, 20)), (17, 17, 17), 30)
              and not near(frame_px(f16, 3.0, (90, 100)), (250, 250, 250), 60)
              and "screens" not in params_of(J16),
              "show your app: the presenter introduces it full frame, then the app's own picture is shown whole (an "
              "app's photos are its screens; none had to be passed)")
    else:
        print("  SKIPPED: the screens are composited with ffmpeg")

    # ------------------------------------------------------------------ 11. a free fix re-renders one beat
    print("== a free fix re-renders only the beat the member points at")
    if FFMPEG:
        os.environ["FREE_FIX"] = "1"
        (J17,), _q = queue(cfg)
        j = run(J17)
        o17 = outputs_of(J17)[0]
        kept = [p for p in parts_of(J17) if p["path"]]
        with db.connect() as conn:
            offer = free_fix.video_offer(conn, ws_row(MWS), job_row(J17))
            img_offer = free_fix.offer(conn, ws_row(MWS), job_row(J17))
        check(j["status"] == "done" and len(kept) == 5 and all(os.path.exists(p["path"]) for p in kept)
              and free_fix.video_fixable(job_row(J17)) and not free_fix.fixable(job_row(J17)) and img_offer is None
              and offer["output"] == o17["id"] and offer["left"] == 2 and offer["cap"] == 2
              and [b["n"] for b in offer["beats"]] == [1, 2, 3] and offer["beats"][0]["name"] == "problem",
              "with FREE_FIX on the beats are kept, and video_offer() lists the parts a member can point at (the "
              "image cards' offer() doesn't change)")
        r = mem.post(f"/app/jobs/{J17}/fix", data={"csrf": csrf(mem), "note": "look at the camera please"},
                     headers={"Accept": "application/json"})
        check(r.status_code == 400 and "which part of the video" in r.get_json()["error"] and "2 (product)" in r.get_json()["error"],
              "a video fix needs to know which part")
        bal, n0, c0 = db.balance(ws_row(MWS)), len(V.submits), len(model_calls)
        r = mem.post(f"/app/jobs/{J17}/fix", data={"csrf": csrf(mem), "note": "look at the camera please", "beat": "1"},
                     headers={"Accept": "application/json"})
        FJ = r.get_json()["job"]
        fj, fp = job_row(FJ), params_of(FJ)
        check(r.status_code == 200 and fj["credits"] == 0 and fj["parent_job_id"] == J17 and fp["free_fix"] is True
              and fp["fix_beat"] == 0 and fp["fix_of"] == J17 and abs(fj["cost_usd"] - 6 * 0.4622) < 1e-3
              and fp["label"].endswith(" · fix") and db.balance(ws_row(MWS)) == bal,
              "the fix is a new 0-credit job for beat 1; what it costs us is that one beat (6 s, $2.77)")
        j = run(FJ)
        subs = V.since(n0)
        check(j["status"] == "done" and len(subs) == 1 and "reference-to-video" in subs[0]["model"]
              and subs[0]["inputs"]["duration"] == 6 and "Correction to the last version, asked for by the advertiser: "
              "look at the camera please" in subs[0]["inputs"]["prompt"] and "not words to say or show" in subs[0]["inputs"]["prompt"]
              and f'They say: "{params_of(J17)["kf"]["beats"][0]["line"]}"' in subs[0]["inputs"]["prompt"]
              and len(model_calls) == c0 and db.balance(ws_row(MWS)) == bal,
              "only that beat is made again, from the kept keyframe, with the note as a correction; no keyframe, no "
              "other beat, no cutaway, no check, no credits")
        ff = outputs_of(FJ)[0]["path"]
        check(abs(stitch.probe(ff)["duration"] - 22.0) < 0.2 and near(frame_px(ff, 8.0, (90, 120)), COLORS["broll"])
              and not near(frame_px(ff, 3.0, (90, 120)), frame_px(o17["path"], 3.0, (90, 120)), 25)
              and near(frame_px(ff, 12.7, (90, 120)), frame_px(o17["path"], 12.7, (90, 120)), 25),
              "the fixed video is the whole video again: a new first beat, the other beats and the cutaway as they were")
        check(all(p["path"] is None for p in parts_of(J17) if p["kind"] != "keyframe")
              and all(p["path"] is None for p in parts_of(FJ) if p["kind"] != "keyframe"),
              "once its fix is made, the original's kept beats are removed")
        r = mem.post(f"/app/jobs/{J17}/fix", data={"csrf": csrf(mem), "note": "one more time please", "beat": "2"},
                     headers={"Accept": "application/json"})
        r2 = mem.post(f"/app/jobs/{FJ}/fix", data={"csrf": csrf(mem), "note": "fix the fix please", "beat": "1"},
                      headers={"Accept": "application/json"})
        check(r.status_code == 400 and "has been used" in r.get_json()["error"] and r2.status_code == 400,
              "one fix per video, and a fix can't itself be fixed")
        (J18,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."], "presenters": ["maya"]})
        run(J18)
        bal = db.balance(ws_row(MWS))
        r = mem.post(f"/app/jobs/{J18}/fix", data={"csrf": csrf(mem), "note": "slower please", "beat": "1"}, headers={"Accept": "application/json"})
        F2 = r.get_json()["job"]
        ticks()
        V.plan[V.n] = {"status": "failed"}
        V.plan[V.n + 1] = {"status": "failed"}
        j = run(F2)
        with db.connect() as conn:
            used = free_fix.video_used_this_month(conn, MWS)
        check(j["status"] == "failed" and j["error"] == free_fix.FAIL_TEXT and used == 1 and db.balance(ws_row(MWS)) == bal,
              "a fix that fails on our side says so, moves no credits and is released (it can be asked for again)")
        r = mem.post(f"/app/jobs/{J18}/fix", data={"csrf": csrf(mem), "note": "slower please", "beat": "1"}, headers={"Accept": "application/json"})
        check(r.status_code == 200, "and it can be")
        run(r.get_json()["job"])
        (J19,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one about the Trail Mug."], "presenters": ["maya"]})
        run(J19)
        r = mem.post(f"/app/jobs/{J19}/fix", data={"csrf": csrf(mem), "note": "slower please", "beat": "1"}, headers={"Accept": "application/json"})
        check(r.status_code == 400 and "This month's 2 free video fixes are used" in r.get_json()["error"]
              and free_fix.cost()["video_monthly_cap"] == 2 and free_fix.cost()["video_fix_usd"] == 3.7,
              "FREE_FIX_VIDEO_MONTHLY_CAP (default 2 a month): a video fix costs us up to $3.70, so it has its own cap")
        with db.connect() as conn:
            old = conn.execute("UPDATE jobs SET finished_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", J19))
            conn.commit()
        kfv._purged["day"] = ""
        n = kfv.maybe_purge()
        check(n >= 1 and all(p["path"] is None for p in parts_of(J19) if p["kind"] != "keyframe")
              and [p for p in parts_of(J19) if p["kind"] == "keyframe"][0]["path"] and kfv.maybe_purge() == 0,
              "kept beats are removed once a video is two weeks old (its keyframe stays); the purge runs once a day")
        os.environ.pop("FREE_FIX")
    else:
        print("  SKIPPED: a fix is stitched with ffmpeg")

    # ------------------------------------------------------------------ 12. cancel
    print("== a member's cancel")
    bal = db.balance(ws_row(MWS))
    V.hold = True
    (J20,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one."], "presenters": ["maya"]})
    ticks()
    with db.connect() as conn:
        ok, msg = worker.cancel_job(conn, job_row(J20))
    check(ok and job_row(J20)["status"] == "failed" and db.balance(ws_row(MWS)) == bal,
          "while only the keyframe is being made, a cancel stops the job and refunds it")
    (J21,), _q = queue({"type": "ugc", "preset": "talk-to-camera", "items": ["Quick one."], "presenters": ["maya"]})
    ticks()
    V.hold = False
    ticks()
    V.hold = True
    ticks()
    with db.connect() as conn:
        ok, msg = worker.cancel_job(conn, job_row(J21))
    check(not ok and "can't be stopped" in msg and job_row(J21)["status"] == "running",
          "once its beats are at the vendor it can't be stopped (they are paid for)")
    V.hold = False
    with db.connect() as conn:
        worker._fail(conn, job_row(J21), worker.CANCELED)

    # ------------------------------------------------------------------ 13. MCP
    print("== MCP")
    mkey = make_key(mem, "write")
    opts = call(mkey, "list_ad_options")["structuredContent"]
    rows = {p["id"]: p for p in opts["presets"]}
    check(rows["problem-solution"]["made_as"] == "keyframe_video" and "not_available_yet" not in rows["problem-solution"]
          and rows["green-screen-product-page"]["needs_screens"] == "a screenshot of your product page"
          and rows["talk-to-camera"]["made_as"] == "keyframe_video" and "made_as" not in rows["offer"],
          "list_ad_options: the recipes are available, say how they are made and which need the member's screens")
    q = call(mkey, "quote_ad_batch", {"ad_type": "ugc", "product_id": PID, "lines": ["This is the page for the mug."],
                                      "preset": "green-screen-product-page", "presenter": "maya", "screen_photo_ids": [PAGE],
                                      "captions": True})
    q2 = call(mkey, "quote_ad_batch", {"ad_type": "ugc", "product_id": PID, "lines": ["This is the page for the mug."],
                                       "preset": "green-screen-product-page", "presenter": "maya"})
    check(not q["isError"] and q["structuredContent"]["total_credits"] == 1387 and q2["isError"]
          and "screenshot of your product page" in q2["content"][0]["text"],
          "quote_ad_batch takes screen_photo_ids and captions; without the screenshot it says what is missing")

    # ------------------------------------------------------------------ 14. the library's cheap paid test
    print("== the library's keyframe test: priced first, cheapest settings, runs with the switch off")
    os.environ.pop("KEYFRAME_VIDEO")
    akey = make_key(adm, "write")

    def lib(kind, ids, **options):
        q = call(akey, "quote_library_assets", {"kind": kind, "ids": ids, "options": options})
        if q["isError"]:
            return q, None, {}
        q = q["structuredContent"]
        g_ = call(akey, "generate_library_assets", {"kind": kind, "ids": ids, "options": options,
                                                     "confirm_total_credits": q["total_credits"]})["structuredContent"]
        return q, g_, {j["key"]: j["job_id"] for j in g_["jobs"]}
    res = call(akey, "quote_library_assets", {"kind": "preset_videos", "ids": ["problem-solution"]})
    check(res["isError"] and "switched off (KEYFRAME_VIDEO)" in res["content"][0]["text"] and "keyframe_tests" in res["content"][0]["text"],
          "with the switch off a pipeline recipe can't be made as an example, and the refusal points at the test")
    _q, _g, lj = lib("demo_products", ["mug", "app"])
    _q, _g, sj = lib("presenter_stills", ["maya:kitchen"])
    for _ in range(4):
        worker.tick()
    qk = call(akey, "quote_library_assets", {"kind": "keyframe_tests", "ids": ["talking-head-cutaways"],
                                             "options": {"presenter": "maya"}})["structuredContent"]
    usd = 12 * 0.2056 + (0.20 + 2 * 0.015) + kfv.check_usd()
    check(qk["count"] == 1 and abs(qk["lines"][0]["usd"] - usd) < 0.011 and qk["total_credits"] == int(-(-usd * 200 // 1)) == 541
          and qk["lines"][0]["seconds"] == 12 and qk["lines"][0]["resolution"] == "480p"
          and qk["lines"][0]["label"] == "Talking head with cutaways · keyframe test · 3 x 4s · 480p"
          and qk["lines"][0]["starts_from"] == f"job {sj['maya:kitchen']}" and round(qk["vendor_usd"], 2) == 2.70,
          f"talking head with cutaways as a test: three 4-second beats at 480p, the packshot as its cutaway, a keyframe "
          f"and its check: {qk['total_credits']} credits, ${qk['vendor_usd']:.2f} at the vendor")
    q6 = call(akey, "quote_library_assets", {"kind": "keyframe_tests", "ids": ["talking-head-cutaways"],
                                             "options": {"presenter": "maya", "video_seconds": 6}})["structuredContent"]
    usd6 = 18 * 0.2056 + 5 * 0.2056 + 0.23 + kfv.check_usd()
    check(q6["lines"][0]["seconds"] == 18 and abs(q6["lines"][0]["usd"] - usd6) < 0.011 and q6["total_credits"] == 993
          and "3 x 6s" in q6["lines"][0]["label"],
          f"with six-second beats (`video_seconds`: one beat's length here) the cutaway is a generated clip too: "
          f"{q6['total_credits']} credits, ${q6['vendor_usd']:.2f}")
    cheap = call(akey, "quote_library_assets", {"kind": "keyframe_tests", "ids": ["talk-to-camera"]})["structuredContent"]
    two = call(akey, "quote_library_assets", {"kind": "keyframe_tests", "ids": ["street-interview"]})["structuredContent"]
    check(cheap["total_credits"] < 220 and cheap["lines"][0]["seconds"] == 4 and 370 < two["total_credits"] < 390
          and two["lines"][0]["seconds"] == 8,
          f"the smallest tests: talk to camera (one beat) {cheap['total_credits']} credits, street interview (two beats) "
          f"{two['total_credits']}")
    res = call(akey, "quote_library_assets", {"kind": "keyframe_tests", "ids": ["green-screen-product-page"]})
    res2 = call(akey, "quote_library_assets", {"kind": "keyframe_tests", "ids": ["offer"]})
    check(res["isError"] and "a screenshot of your product page" in res["content"][0]["text"] and res2["isError"],
          "a recipe the demo products can't show (no page screenshot) is refused with the reason; so is a preset that "
          "isn't a video made in beats")
    if FFMPEG:
        n0, bal = len(V.submits), db.balance(ws_row(AWS))
        qt, gt, tj = lib("keyframe_tests", ["talking-head-cutaways"], presenter="maya")
        KT = tj["talking-head-cutaways"]
        pk = params_of(KT)
        check(not kfv.on() and pk["pipeline"] == "keyframe_video" and pk["beat_seconds"] == 4 and pk["resolution"] == "480p"
              and pk["library"] == {"kind": "keyframe_test", "preset": "talking-head-cutaways", "demo": "mug",
                                    "presenter": "maya", "situation": "kitchen"}
              and [b["seconds"] for b in pk["kf"]["beats"]] == [4, 4, 4] and pk["ref_job"] == sj["maya:kitchen"]
              and all(len(b["line"].split()) <= catalog.max_words(4) and b["line"] for b in pk["kf"]["beats"])
              and gt["credits_charged"] == qt["total_credits"] == bal - db.balance(ws_row(AWS)),
              "the test job carries the pipeline itself (the switch is off), four-second beats with lines that fit, "
              "480p, the presenter's finished still as the keyframe's first reference")
        j = run(KT)
        subs = V.since(n0)
        vids = [s for s in subs if "reference-to-video" in s["model"]]
        check(j["status"] == "done" and len(subs[0]["inputs"]["image_urls"]) == 2
              and [v["inputs"]["duration"] for v in vids] == [4, 4, 4] and all(v["inputs"]["resolution"] == "480p" for v in vids)
              and not [s for s in subs if "image-to-video" in s["model"]] and pk["kf"]["cutaways"][0]["kind"] == "still"
              and abs(stitch.probe(outputs_of(KT)[0]["path"])["duration"] - 14.0) < 0.2
              and not near(frame_px(outputs_of(KT)[0]["path"], 4.0 + 0.8 + 1.4, (90, 120)), frame_px(outputs_of(KT)[0]["path"], 4.3, (90, 120)), 25),
              "it runs end to end with KEYFRAME_VIDEO off: keyframe, three beats at 480p, the packshot laid over the "
              "second beat, stitched to 14 s")
        lst = {a["job_id"]: a for a in call(akey, "list_library_assets", {"kind": "keyframe_tests"})["structuredContent"]["assets"]}
        page = adm.get("/admin/library?show=keyframe_tests").get_data(as_text=True)
        r = adm.get(f"/admin/library/keyframe/{KT}")
        check(lst[KT]["filename"] == "keyframe_test__talking-head-cutaways.mp4" and lst[KT]["kind"] == "keyframe_test"
              and "keyframe tests" in page and f"/admin/library/keyframe/{KT}" in page and r.status_code == 200
              and r.data[:4] == b"\x89PNG" and mem.get(f"/admin/library/keyframe/{KT}").status_code == 403
              and adm.get("/admin/library/keyframe/999999").status_code == 404,
              "it is listed with the library's assets, and the admin page shows its keyframe")
        _q, _g, aj = lib("keyframe_tests", ["show-your-app"], presenter="maya")
        check(run(aj["show-your-app"])["status"] == "done", "show your app runs on the demo app (its packshot is its screen)")
    else:
        print("  SKIPPED: the test's video is stitched with ffmpeg")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "quote", "kind": "keyframe_tests", "ids": "street-interview",
                                         "presenter": "maya"})
    check(f"1 job: {two['total_credits']} credits" in r.get_data(as_text=True) and "2 x 4s" in r.get_data(as_text=True),
          "and it is quoted from /admin/library")
    os.environ["KEYFRAME_VIDEO"] = "1"
    q = call(akey, "quote_library_assets", {"kind": "preset_videos", "ids": ["problem-solution"], "options": {"presenter": "maya"}})
    q2 = call(akey, "quote_library_assets", {"kind": "preset_videos", "ids": ["problem-solution"],
                                             "options": {"presenter": "maya", "video_seconds": 12, "video_resolution": "480p"}})
    check(not q["isError"] and q["structuredContent"]["total_credits"] == 1849 and q["structuredContent"]["vendor_usd"] > 11.5
          and q2["structuredContent"]["lines"][0]["seconds"] == 12 and q2["structuredContent"]["total_credits"] == 494,
          "with the switch on the pipeline's recipes can be made as library examples too, at their member price (or "
          "the library's own length and size)")
    os.environ.pop("KEYFRAME_VIDEO")

    # ------------------------------------------------------------------ 15. the pieces
    print("== the pieces: captions, tags, the end card, the stitcher")
    ch = videotext.caption_chunks("Still putting up with a lid that goes missing? This one presses in, and stays.", 7)
    check(len(ch) == 4 and ch[0]["start"] >= 0.1 and ch[-1]["end"] <= 6.9 and all(a["end"] == b["start"] for a, b in zip(ch, ch[1:]))
          and all(len(c["text"].split()) <= 5 for c in ch) and " ".join(c["text"] for c in ch).split() == (
              "Still putting up with a lid that goes missing? This one presses in, and stays.").split()
          and videotext.caption_chunks("", 5) == [] and len(videotext.caption_chunks("Meet the mug.", 4)) == 1,
          "captions: the line in chunks of a few words, each on screen for its share of the beat, none lost")
    size = (720, 1280)
    safe = textlayer.safe_box(size, "9:16")
    low = videotext.caption_png("Still putting up with", size, os.path.join(TMP, "cap.png"), aspect="9:16")
    high = videotext.caption_png("Still putting up with", size, os.path.join(TMP, "cap2.png"), aspect="9:16", high=True)
    tag = videotext.tag_png("Step 1", size, os.path.join(TMP, "tag.png"), aspect="9:16")
    lab = videotext.tag_png("AI presenter", size, os.path.join(TMP, "lab.png"), aspect="9:16", corner="right", accent=False)
    inside = lambda b: b[0] >= safe[0] - 1 and b[1] >= safe[1] - 1 and b[2] <= safe[2] + 1 and b[3] <= safe[3] + 1  # noqa: E731
    with Image.open(os.path.join(TMP, "cap.png")) as im:
        cap_im = im.convert("RGBA")
    check(all(inside(b) for b in (low, high, tag, lab)) and low[3] == safe[3] and high[1] < size[1] * 0.3
          and tag[0] == safe[0] and lab[2] == safe[2] and cap_im.size == size and cap_im.getpixel((5, 5))[3] == 0
          and cap_im.getpixel(((low[0] + low[2]) // 2, low[1] + 6))[3] > 100,
          "captions, tags and labels are transparent full-frame pictures drawn inside the 9:16 safe area (clear of "
          "the top 14% and the bottom 20%)")
    card_path = os.path.join(TMP, "card.png")
    rep = videotext.end_card(size, card_path, packshot=photo_path, aspect="9:16", headline="Trail Mug", subline="Northwind",
                             badge="Free lid with every mug", cta="Shop now", kit={"primary": "#1F4D3A", "accent": "#F5B301"})
    with Image.open(card_path) as im:
        card = im.convert("RGB")
    rep2 = videotext.end_card((720, 720), os.path.join(TMP, "card2.png"), packshot=None, aspect="1:1", headline="Planner",
                              cta=videotext.cta_for("app_service"))
    check(rep["ok"] and {"headline", "cta", "badge"} <= set(rep["layers"]) and rep["packshot"] and card.size == size
          and near(card.getpixel((6, 6)), (31, 77, 58), 12) and rep["compose"]["clear"]
          and all(inside(v["box"]) for v in rep["layers"].values()) and rep2["ok"] and "badge" not in rep2["layers"]
          and videotext.cta_for("app_service") == "Try it free" and videotext.cta_for("home") == "Shop now"
          and 1.5 <= kfv.END_CARD_SECONDS <= 2.5,
          "the end card: the real packshot on the brand colour, the product's name, its saved offer as the badge, "
          "a button, all set by the text layer inside the safe area and off the product")
    if FFMPEG:
        a, b = os.path.join(TMP, "a.mp4"), os.path.join(TMP, "b.mp4")
        open(a, "wb").write(clip((200, 30, 30), 4))
        open(b, "wb").write(clip((30, 160, 60), 4, quiet=True, size=(200, 356)))
        size_, fps = stitch.standard(a)
        na = stitch.render_beat(a, os.path.join(TMP, "na.mp4"), size=size_, fps=fps, seconds=4)
        nb = stitch.render_beat(b, os.path.join(TMP, "nb.mp4"), size=size_, fps=fps, seconds=3)
        hard = stitch.concat([os.path.join(TMP, "na.mp4"), os.path.join(TMP, "nb.mp4")], os.path.join(TMP, "hard.mp4"), fps=fps)
        soft = stitch.concat([os.path.join(TMP, "na.mp4"), os.path.join(TMP, "nb.mp4")], os.path.join(TMP, "soft.mp4"), fps=fps,
                             crossfade_frames=6)
        mixed = frame_px(os.path.join(TMP, "soft.mp4"), 4.0 - 0.125, (90, 160))
        check(size_ == (180, 320) and fps == 24 and (nb["width"], nb["height"]) == (180, 320) and abs(nb["duration"] - 3.0) < 0.1
              and abs(hard["duration"] - 7.0) < 0.15 and abs(soft["duration"] - (7.0 - 6 / 24)) < 0.15
              and near(frame_px(os.path.join(TMP, "hard.mp4"), 3.9, (90, 160)), (200, 30, 30))
              and near(frame_px(os.path.join(TMP, "hard.mp4"), 4.1, (90, 160)), (30, 160, 60))
              and not near(mixed, (200, 30, 30), 30) and not near(mixed, (30, 160, 60), 30),
              "the stitcher: a clip of another size is brought to the first beat's and cut to its planned length; "
              "hard cuts by default, a six-frame crossfade when the recipe asks")
        silent = os.path.join(TMP, "silent.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=180x320:r=24:d=2", "-pix_fmt",
                        "yuv420p", silent], check=True)
        ns = stitch.render_beat(silent, os.path.join(TMP, "ns.mp4"), size=size_, fps=fps, seconds=2)
        fr = stitch.frames(os.path.join(TMP, "hard.mp4"), os.path.join(TMP, "fr"), 3)
        try:
            stitch.probe(os.path.join(TMP, "missing.mp4"))
            err = ""
        except stitch.StitchError as e:
            err = str(e)
        check(ns["audio"] and len(fr) == 3 and [round(t / hard["duration"], 2) for t, _p in fr] == [0.15, 0.5, 0.85]
              and all(os.path.getsize(p) > 200 for _t, p in fr) and "couldn't be read" in err,
              "a clip with no sound gets silence (so every part has a sound track); frames() cuts three frames at "
              "15%, 50% and 85%; a file that isn't there raises StitchError")

    # ------------------------------------------------------------------ 16. deleting the workspace
    print("== deleting a workspace deletes every part's vendor request too")
    import wsdelete
    with db.connect() as conn:
        rids = [r["request_id"] for r in conn.execute("SELECT request_id FROM video_parts WHERE workspace_id=? AND "
                                                      "request_id IS NOT NULL", (MWS,))]
    busy, asked = rids[0], []

    def fake_delete(rid):
        asked.append(rid)
        return hf.BUSY if rid == busy else hf.DELETED
    hf.delete_request = fake_delete
    with db.connect() as conn:
        res = wsdelete.purge(conn, ws_row(MWS), tries=1)
        left = conn.execute("SELECT * FROM video_parts WHERE workspace_id=?", (MWS,)).fetchall()
    check(len(rids) > 20 and set(rids) <= set(asked) and res["done"] is False and res["vendor"]["left"] == 1
          and [r["request_id"] for r in left] == [busy] and left[0]["inputs_json"] is None and left[0]["path"] is None
          and not os.path.exists(os.path.join(worker.MEDIA, str(MWS))),
          "every keyframe, beat and cutaway request is deleted at the vendor; one the vendor is still busy with keeps "
          "its row (and nothing else) so the next run tries it again")
    busy = None
    with db.connect() as conn:
        res = wsdelete.purge(conn, ws_row(MWS), tries=2)
        left = conn.execute("SELECT COUNT(*) FROM video_parts WHERE workspace_id=?", (MWS,)).fetchone()[0]
        other = conn.execute("SELECT COUNT(*) FROM video_parts WHERE workspace_id=?", (AWS,)).fetchone()[0]
    check(res["done"] is True and left == 0 and (other > 0 or not FFMPEG),
          "the next run finishes it; another workspace's parts are untouched")

    print("== no vendor was really called")
    check(leaks == [], "no connection left this machine during the whole suite")
    if not FFMPEG:
        print("\nNOTE: ffmpeg isn't installed, so the stitching checks were skipped.")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} keyframe video checks passed")

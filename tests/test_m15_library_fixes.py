"""What the first paid library runs showed, fixed (m15), on a throwaway database.

  * a static with a text layer shows the product alone: the image model is told "no people, no hands, no faces"
    (the six older layered statics and m15's); the text layer finds a face with a local heuristic and keeps its words
    off it where a part can move; a picture whose words can't be moved off a face is made again once, silently,
    with or without IMAGE_QA; with IMAGE_QA on, a product-only picture the vision check says has people in it fails
    (silent retry, then refund)
  * a "same person, somewhere new" still that the vendor's content filter refuses is tried once more with a softer
    prompt (no wardrobe adjectives, a shorter description) before it is given up; the vendor's reason is kept for
    the admin's library page and tools, never shown to members
  * the bathroom-mirror situation has a modest wardrobe for every presenter who has it, and says so in its prompt

No network: every vendor is faked, and a socket that tries to leave the machine fails the test.

    python tests/test_m15_library_fixes.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_m15fix_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("FIRST_RUN_NUDGES", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "RS_FREE_PREVIEW", "RS_TALKING", "RS_SERVICE_PATH",
          "CLAIMS_CHECK", "IMAGE_QA", "IMAGE_QA_DAILY_MAX", "IMAGE_QA_MODEL", "FREE_FIX", "TEXT_LAYERS", "RS_MARKUP",
          "RS_HD_MARKUP", "PRESENTER_STILLS", "PRESENTER_VOICE_REF", "RS_LIBRARY_STILL_MODEL", "RS_LIBRARY_STILL_USD",
          "RS_CARD_REQUIRED", "UI_V2", "ANTHROPIC_FAST_MODEL", "LLM_USD_IN_PER_MTOK", "LLM_USD_OUT_PER_MTOK", "GROQ_API_KEY"):
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
import hf  # noqa: E402
import image_qa  # noqa: E402
import llm  # noqa: E402
import presenters  # noqa: E402
import presets  # noqa: E402
import products  # noqa: E402
import textlayer  # noqa: E402
import worker  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

passed = 0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


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
    c.environ_base["REMOTE_ADDR"] = f"10.33.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


# ---------------------------------------------------------------- pictures
def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def outdoors(size=(1080, 1350)):
    """A scene, not a backdrop: sky over grass (nothing in it is skin-coloured)."""
    W, H = size
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(110 + 30 * t), int(170 - 60 * t), int(225 - 150 * t)) if t < 0.55
               else (60, int(140 - 40 * t), 70))
    return im, d


def draw_face(d, cx, cy, r, tone=(224, 172, 140)):
    d.ellipse((cx - r * 1.05, cy - r * 1.5, cx + r * 1.05, cy - r * 0.3), fill=(40, 30, 25))            # hair
    d.ellipse((cx - r, cy - r * 1.25, cx + r, cy + r * 1.25), fill=tone)
    for ex in (-0.42, 0.42):
        d.ellipse((cx + ex * r - r * 0.16, cy - r * 0.34, cx + ex * r + r * 0.16, cy - r * 0.16), fill=(30, 25, 25))
        d.line((cx + ex * r - r * 0.22, cy - r * 0.45, cx + ex * r + r * 0.22, cy - r * 0.47), fill=(50, 35, 30),
               width=max(2, int(r * 0.07)))
    d.ellipse((cx - r * 0.35, cy + r * 0.55, cx + r * 0.35, cy + r * 0.72), fill=(150, 70, 70))


def man_with_mug(size=(1080, 1350), cx=540, cy=330, r=150, tone=(224, 172, 140)):
    """What the image model made for "the product centred on a clean surface": a man outdoors holding the mug."""
    im, d = outdoors(size)
    d.rectangle((cx - r * 1.6, cy + r * 1.3, cx + r * 1.6, size[1]), fill=(60, 80, 140))                 # his shirt
    draw_face(d, cx, cy, r, tone)
    d.rectangle((cx + r, cy + r * 2.2, cx + r * 2.1, cy + r * 3.6), fill=(40, 90, 70))                    # the mug
    return im.filter(ImageFilter.GaussianBlur(1.2))


def packshot(size=(1080, 1350)):
    """What was asked for: the product alone, low in the frame, on a plain backdrop."""
    im = Image.new("RGB", size, (236, 238, 240))
    d = ImageDraw.Draw(im)
    W, H = size
    d.rectangle((W * 0.38, H * 0.52, W * 0.62, H * 0.80), fill=(40, 90, 70))
    d.rectangle((W * 0.42, H * 0.60, W * 0.58, H * 0.68), fill=(236, 228, 205))
    return im


# ---------------------------------------------------------------- the fake vendors
class Vendor:
    """submit returns a request id; status answers what `plan` holds for that request number (a picture, "nsfw" with
    the vendor's words, ...), else a plain packshot / clip."""

    def __init__(self):
        self.submits, self.uploads, self.n, self.files, self.plan = [], [], 0, {}, {}

    def upload(self, data, ctype):
        self.uploads.append({"bytes": len(data), "type": ctype})
        return f"https://files.example/up{len(self.uploads)}"

    def submit(self, model, inputs, webhook_url=None, idempotency_key=None):
        self.n += 1
        self.submits.append({"model": model, "inputs": inputs, "key": idempotency_key})
        return f"req-{self.n}"

    def next(self, *answers):
        """What the next requests (in order) get back."""
        for i, a in enumerate(answers, start=1):
            self.plan[self.n + i] = a

    def status(self, rid):
        n = int(rid.split("-")[1])
        ans = self.plan.get(n)
        if isinstance(ans, dict):
            return ans
        pics = ans if isinstance(ans, list) else [ans if ans is not None else packshot()]
        want = int(self.submits[n - 1]["inputs"].get("batch_size") or 1)
        out = []
        for i in range(want):
            url = f"https://cdn.example/{rid}-{i}.png"
            self.files[url] = png(pics[i % len(pics)])
            out.append({"url": url})
        return {"status": "completed", "images": out}


V = Vendor()
hf.upload, hf.submit, hf.status = V.upload, V.submit, V.status
hf.estimate = lambda model, inputs: None
hf.cancel = lambda rid: hf.CANCELED


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
worker.make_poster = lambda path: None
model_calls, answers = [], []
GOOD = {"product_present": True, "photo_has_label": True, "label_matches": True, "label_legible": True, "other_text": [],
        "garbled_text": False, "people": False, "deformed_hands": False, "deformed_faces": False, "notes": ""}


def _fake_anthropic(body, timeout=None):
    model_calls.append(body)
    if not answers:
        raise AssertionError("an unexpected model call: " + body["tools"][0]["name"])
    return {"model": body["model"], "usage": {"input_tokens": 2600, "output_tokens": 250},
            "content": [{"type": "tool_use", "name": body["tools"][0]["name"], "input": answers.pop(0)}]}


llm._anthropic = _fake_anthropic
STATIC = os.path.join(TMP, "static")
os.makedirs(os.path.join(STATIC, "presenters"))
os.makedirs(os.path.join(STATIC, "presets"))
presenters.STATIC_DIR = os.path.join(STATIC, "presenters")
presets.EXAMPLE_DIR = os.path.join(STATIC, "presets")
worker.STATIC = STATIC
presenters.HERE = TMP


def tick_until_idle(limit=12):
    for _ in range(limit):
        if not worker.tick():
            break


try:
    # ------------------------------------------------------------------ 1. the prompt: nobody in a layered static
    print("== a static with a text layer is told to show nobody")
    named = ["clean-headline", "offer", "callouts", "review", "compare", "marketplace", "problem-answer", "three-reasons",
             "how-it-works", "then-now", "app-screen"] + [p.id for p in presets.STATICS_M15 if p.id.startswith("season-")]
    check(all(presets.product_only(i) for i in named) and len(named) == 19
          and {p.id for p in presets.layered()} == set(named),
          "all 19 statics with a text layer are product-only (the six older ones and m15's thirteen)")
    check(not any(presets.product_only(i) for i in ("hero", "routine", "inhand", "street", "image", "ugc", "talk-to-camera",
                                                     "nope", "", None)),
          "the image model's own templates (a hand holding it, a person on the street) and the videos are not")
    brandk = {"name": "Northwind", "product": "Trail Mug"}
    for pid in named:
        p = presets.get(pid)
        scene = presets.layered_scene(catalog.split_image_brief(catalog.fill(p.recipe, brandk))["scene"], p.layout, p.id)
        prompt = catalog.image_ad_prompt(scene, product="Trail Mug")
        if presets.NO_PEOPLE not in prompt or prompt.count("No people") != 1 or "Trail Mug" not in prompt.split("\n")[0]:
            check(False, f"{pid}: its prompt says nobody is in the picture ({prompt[:200]})")
    check(presets.NO_PEOPLE == "No people, no hands, no faces and no body parts anywhere in the picture.",
          "each one's prompt ends its scene with: no people, no hands, no faces")
    with_person = presets._from_layered(dict(presets._LAYERED[0], id="x-person", people=True, preview="p"))
    presets.BY_ID["x-person"] = with_person
    check(not presets.product_only("x-person")
          and presets.NO_PEOPLE not in presets.layered_scene("A person holds it.", "headline", "x-person")
          and presets.ZONES["headline"] in presets.layered_scene("A person holds it.", "headline", "x-person")
          and presets.NO_PEOPLE in presets.layered_scene("The mug.", "headline", "gone-preset"),
          "a record can opt out with people=True (none does); an unknown preset is treated as product-only")
    del presets.BY_ID["x-person"]

    # ------------------------------------------------------------------ 2. the text layer keeps off a face
    print("== the text layer finds a face and keeps its words off it")
    words = {"headline": "The mug that goes with you", "cta": "Get yours", "badge": "$24"}
    for tone in ((224, 172, 140), (255, 219, 172), (198, 134, 66), (141, 85, 36), (92, 56, 36)):
        for size in ((1080, 1350), (1080, 1920), (1080, 1080)):
            found = textlayer.locate_people(man_with_mug(size, tone=tone))
            if len(found) != 1 or not (found[0][0] < 540 - 140 and found[0][2] > 540 + 140 and found[0][1] < 150
                                       and found[0][3] > 330 + 170):
                check(False, f"a face of tone {tone} at {size} is found ({found})")
    check(True, "a face is found in five skin tones and three shapes; its box takes in the hair and the chin")
    check(all(textlayer.locate_people(man_with_mug(cy=500, r=r)) for r in (60, 80, 110, 200, 260)),
          "from a face a ninth of the frame wide to one that fills half of it")
    pic = man_with_mug()
    out, rep = textlayer.render(pic, "offer", words)
    c = rep["compose"]
    check(c["product"] is None and len(c["people"]) == 1 and c["people_clear"] is False and rep["ok"] is False
          and textlayer._overlap(rep["layers"]["headline"]["box"], c["people"][0]),
          "the reported case: a man where the headline goes. No part can be moved off his face, and the report says so")
    low = man_with_mug(cy=1120, r=110)
    _o, rep = textlayer.render(low, "headline", words)
    c = rep["compose"]
    check(c["people_clear"] is True and rep["ok"] and c["action"] == "moved" and c["place"] == {"cta": "right"}
          and not any(textlayer._overlap(v["box"], c["people"][0]) for v in rep["layers"].values()),
          "a face where the button goes: the button moves beside it, and nothing is on the face")
    side = man_with_mug(cx=860, cy=760, r=110)
    _o, rep = textlayer.render(side, "offer", words)
    c = rep["compose"]
    check(c["people_clear"] is True and c["place"].get("badge", ("",))[0] == "left"
          and not any(textlayer._overlap(v["box"], c["people"][0]) for v in rep["layers"].values()),
          "a face where the badge goes: the badge moves to the other side")
    # nothing changes for pictures with nobody in them
    clean = packshot()
    _o, rep = textlayer.render(clean, "offer", words)
    wood = Image.new("RGB", (1080, 1350), (232, 214, 190))                 # a beige wall, a wooden table, kraft parcels
    d = ImageDraw.Draw(wood)
    d.rectangle((0, 900, 1080, 1350), fill=(150, 105, 70))
    d.rectangle((120, 700, 420, 980), fill=(200, 160, 110))
    d.line((120, 840, 420, 840), fill=(120, 90, 60), width=6)
    d.rectangle((620, 650, 900, 990), fill=(60, 90, 70))
    bowl, bd = outdoors()
    bd.ellipse((300, 600, 700, 900), fill=(205, 165, 120))
    check("people" not in rep["compose"] and rep["compose"]["product"] and rep["ok"]
          and textlayer.locate_people(clean) == [] and textlayer.locate_people(wood) == []
          and textlayer.locate_people(bowl) == [] and textlayer.locate_people(Image.new("RGB", (1080, 1350), (200, 160, 120))) == []
          and textlayer.locate_people(Image.new("RGB", (40, 40), (224, 172, 140))) == [],
          "a packshot, a beige wall with a wooden table and kraft parcels, a plain bowl and a skin-coloured backdrop "
          "have nobody in them: they are laid out exactly as before")
    kraft = Image.new("RGB", (1080, 1350), (240, 240, 238))                # a kraft bag with a dark label on a plain backdrop
    kd = ImageDraw.Draw(kraft)
    kd.ellipse((380, 700, 700, 1100), fill=(205, 165, 120))
    kd.rectangle((450, 820, 630, 900), fill=(40, 60, 40))
    _o, rep = textlayer.render(kraft, "headline", words)
    check(rep["compose"]["product"] and "people" not in rep["compose"] and rep["ok"],
          "a skin-coloured product on a plain backdrop is the product, not a person")
    _o, rep = textlayer.render(pic, "offer", words, people=[])
    _o, rep2 = textlayer.render(clean, "headline", words, people=[(380, 60, 700, 300)])
    check("people" not in rep["compose"] and rep["ok"] and rep2["compose"]["people_clear"] is False,
          "the boxes can be given instead of looked for")
    face_path, clean_path = os.path.join(TMP, "face.png"), os.path.join(TMP, "clean.png")
    pic.save(face_path)
    clean.save(clean_path)
    check(textlayer.words_on_people(face_path, "offer", words) is True
          and textlayer.words_on_people(clean_path, "offer", words) is False
          and textlayer.words_on_people(face_path, "none", words) is False
          and textlayer.words_on_people(os.path.join(TMP, "missing.png"), "offer", words) is False
          and not os.path.exists(textlayer.base_path(face_path)),
          "words_on_people(): yes for the man, no for the packshot, no for a layout with no words; it writes nothing")

    # ------------------------------------------------------------------ an admin and a member with a product
    adm, au = signup("owner@example.com", credits=100000, admin=True, plan="scale")
    mem, mu = signup("member@example.com", credits=5000)
    AWS, MWS = au["workspace_id"], mu["workspace_id"]
    photo_path = os.path.join(TMP, "product.png")
    packshot((600, 750)).save(photo_path)
    with appmod.app.test_request_context("/"):
        from flask import g
        with db.connect() as conn:
            g.user = conn.execute("SELECT * FROM users WHERE id=?", (mu["id"],)).fetchone()
            g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (MWS,)).fetchone()
            PID = products.save_kit(conn, g.ws, brand_name="Northwind", product_name="Trail Mug", audience="Hikers", claims="")
            products.add_photo(conn, MWS, PID, photo_path, "product.png", "image/png")
            conn.execute("UPDATE products SET price='$24' WHERE id=?", (PID,))
            conn.commit()

    def make_offer(versions=1):
        scene = catalog.template_prefill(presets.get("offer").as_template(), {"name": "Northwind", "product": "Trail Mug"})
        cfg = {"product": PID, "type": "image", "preset": "offer", "items": ["The mug that goes with you"],
               "body": scene["body"], "aspects": ["4:5"], "versions": versions, "cta": "Get yours",
               "layers": {"badge": "$24"}}
        q = post(mem, "/app/create/quote", cfg).get_json()
        r = post(mem, "/app/create/generate", dict(cfg, total=q["total"])).get_json()
        with db.connect() as conn:
            return conn.execute("SELECT id FROM jobs WHERE batch_id=?", (r["batch"],)).fetchone()["id"], q["total"]

    # ------------------------------------------------------------------ 3. the worker: made again once, silently
    print("== a picture with a person where the words go is made again once, silently (IMAGE_QA off)")
    order = []
    _apply = textlayer.apply_to_file

    def _apply_noting(path, *a, **k):
        order.append("layers")
        return _apply(path, *a, **k)
    textlayer.apply_to_file = _apply_noting
    _anth = llm._anthropic

    def _anth_noting(body, timeout=None):
        order.append("check")
        return _anth(body, timeout)
    llm._anthropic = _anth_noting
    check(not image_qa.on(), "(IMAGE_QA is off for this part)")
    bal = db.balance(ws_row(MWS))
    V.next(man_with_mug(), packshot())
    answers[:] = [GOOD]
    jid, price = make_offer()
    worker.tick()            # one pass: sent, finished at the vendor, looked at
    sent = V.submits[-1]["inputs"]["prompt"]
    check(presets.NO_PEOPLE in sent and presets.ZONES["offer"] in sent and "Trail Mug centred" in sent,
          "the worker's prompt for the offer static says nobody is in the picture")
    j = job_row(jid)
    with db.connect() as conn:
        qa = conn.execute("SELECT * FROM asset_qa WHERE job_id=?", (jid,)).fetchall()
    check(j["status"] == "pending" and params_of(jid)["qa_attempt"] == 2 and outputs_of(jid) == [] and j["error"] is None
          and len(qa) == 1 and qa[0]["verdict"] == "fail" and qa[0]["model"] == "textlayer" and qa[0]["usd"] == 0
          and json.loads(qa[0]["reasons_json"]) == [textlayer.PEOPLE_REASON] and model_calls == []
          and db.balance(ws_row(MWS)) == bal - price and not os.path.exists(qa[0]["path"]),
          "the man is not delivered: the job goes back for one more try, no vision call is spent, nothing is refunded")
    tick_until_idle()
    j, outs = job_row(jid), outputs_of(jid)
    done = Image.open(outs[0]["path"]).convert("RGB")
    check(j["status"] == "done" and len(outs) == 1 and len(V.submits) == 2 and j["credits"] == price == 22
          and abs(j["cost_usd"] - 2 * 0.1071) < 1e-6 and db.balance(ws_row(MWS)) == bal - price
          and done.size == (1080, 1350) and done.tobytes() != packshot().tobytes()
          and os.path.exists(textlayer.base_path(outs[0]["path"])),
          "the second picture (the product alone) is delivered with its words; the member paid once, we paid twice")
    # the second try: the heuristic alone never drops a picture; the vision check decides
    V.next(man_with_mug(), man_with_mug())
    answers[:] = [GOOD]                    # the model looked and saw nobody: the heuristic was wrong, the picture is kept
    jid, price = make_offer()
    tick_until_idle()
    j = job_row(jid)
    check(j["status"] == "done" and len(outputs_of(jid)) == 1 and j["credits"] == price and j["error"] is None and answers == [],
          "a second picture the heuristic also dislikes is delivered when the vision check sees nobody in it")
    bal = db.balance(ws_row(MWS))
    V.next(man_with_mug(), man_with_mug())
    answers[:] = [dict(GOOD, people=True)]
    jid, price = make_offer()
    tick_until_idle()
    j = job_row(jid)
    check(j["status"] == "failed" and image_qa.FAIL_TEXT in j["error"] and outputs_of(jid) == []
          and db.balance(ws_row(MWS)) == bal and answers == [],
          "the man twice (the vision check agrees the second time): never delivered, credits returned")
    # two versions, one of them the man: the good one waits, the other is made again
    V.next([packshot(), man_with_mug()], packshot())
    answers[:] = [GOOD, GOOD]
    jid, price = make_offer(versions=2)
    worker.tick()
    with db.connect() as conn:
        qa = conn.execute("SELECT verdict, kept, idx FROM asset_qa WHERE job_id=? ORDER BY idx", (jid,)).fetchall()
    check([(r["verdict"], r["kept"], r["idx"]) for r in qa] == [("pass", 1, 1), ("fail", 0, 2)]
          and params_of(jid)["batch"] == 1 and params_of(jid)["qa_want"] == 2 and job_row(jid)["status"] == "pending",
          "of two versions, the good one is held back and only the other is made again")
    tick_until_idle()
    check(job_row(jid)["status"] == "done" and len(outputs_of(jid)) == 2 and job_row(jid)["credits"] == price
          and V.submits[-1]["inputs"]["batch_size"] == 1 and answers == [], "and both are delivered")
    # an image ad whose words the image model sets is untouched by all this
    n_calls = len(model_calls)
    V.next(man_with_mug())
    cfg = {"product": PID, "type": "image", "items": ["Take it with you"], "body": "A hand holds the Trail Mug outdoors.",
           "aspects": ["4:5"], "versions": 1}
    q = post(mem, "/app/create/quote", cfg).get_json()
    r = post(mem, "/app/create/generate", dict(cfg, total=q["total"])).get_json()
    n_sub = len(V.submits)
    tick_until_idle()
    with db.connect() as conn:
        pj = conn.execute("SELECT * FROM jobs WHERE batch_id=?", (r["batch"],)).fetchone()
    plain = V.submits[-1]["inputs"]["prompt"]
    check(pj["status"] == "done" and len(V.submits) == n_sub + 1 and presets.NO_PEOPLE not in plain
          and "A hand holds" in plain and plain.startswith("Advertising image. Scene:") and catalog.TEXT_FREE not in plain
          and '- Headline (the main text, large and easy to read): "Take it with you"' in plain
          and len(model_calls) == n_calls,
          "an ordinary image ad may show a person and carries its headline: its prompt is what it was, it is "
          "delivered as it comes, and with IMAGE_QA off nothing looks at it")

    # ------------------------------------------------------------------ 3b. words the image model painted in
    print("== a layered static's text-free picture is always checked for words the image model painted in")
    check(sent.startswith("Product photograph with no text on it. Scene:") and "Advertising image" not in sent
          and "Text on the image: none at all." in sent and catalog.TEXT_FREE in sent and catalog.NO_EXTRA_TEXT in sent
          and "No words, letters, numbers, captions, logos or graphics anywhere" in catalog.TEXT_FREE
          and "No infographic elements" in catalog.TEXT_FREE and "The mug that goes with you" not in sent
          and "$24" not in sent and "Get yours" not in sent,
          "the image model is asked for a photograph with no words, letters, numbers, captions, logos or graphics "
          "except the product's own label, and no infographic elements; none of the ad's words are in its prompt")
    INVENTED = ["Every morning starts here", "one scoop, one glass, done", "30 servings"]
    j_ = image_qa.judge(dict(GOOD, other_text=INVENTED, deformed_hands=True, label_matches=False), only=image_qa.LAYERED_CHECKS)
    j2 = image_qa.judge(dict(GOOD, deformed_hands=True, label_matches=False), only=image_qa.LAYERED_CHECKS)
    j3 = image_qa.judge(dict(GOOD, deformed_hands=True, label_matches=False))
    check(image_qa.LAYERED_CHECKS == ("no_extra_text", "no_people") and not j_["pass"] and len(j_["reasons"]) == 1
          and j_["reasons"][0].startswith("Extra text: Every morning starts here") and j_["checks"]["no_deformed_hands_faces"] is False
          and j2["pass"] and j2["reasons"] == [] and not j3["pass"] and len(j3["reasons"]) == 2
          and not image_qa.judge(dict(GOOD, garbled_text=True), only=image_qa.LAYERED_CHECKS)["pass"]
          and not image_qa.judge(dict(GOOD, people=True), no_people=True, only=image_qa.LAYERED_CHECKS)["pass"],
          "judge(only=...): only invented words (or a person) fail the picture; the other answers are recorded, not judged")
    del order[:]
    bal, n_calls = db.balance(ws_row(MWS)), len(model_calls)
    V.next(packshot(), packshot())
    answers[:] = [dict(GOOD, other_text=INVENTED), GOOD]
    jid, price = make_offer()
    worker.tick()
    with db.connect() as conn:
        qa = conn.execute("SELECT * FROM asset_qa WHERE job_id=? ORDER BY id", (jid,)).fetchall()
    body = model_calls[-1]
    said = json.dumps(body["messages"])
    check(job_row(jid)["status"] == "pending" and params_of(jid)["qa_attempt"] == 2 and outputs_of(jid) == []
          and len(qa) == 1 and qa[0]["verdict"] == "fail" and "Extra text: Every morning starts here" in qa[0]["reasons_json"]
          and qa[0]["usd"] > 0 and order == ["check"] and db.balance(ws_row(MWS)) == bal - price
          and "meant to carry no text at all" in said and "The mug that goes with you" not in said,
          "IMAGE_QA is off, and the picture with invented copy is still caught: not delivered, made again, and the "
          "check is told the picture should carry no words (it never sees our own layers)")
    tick_until_idle()
    check(job_row(jid)["status"] == "done" and len(outputs_of(jid)) == 1 and order == ["check", "check", "layers"]
          and len(model_calls) == n_calls + 2 and answers == [] and job_row(jid)["credits"] == price,
          "the second picture is clean: it is checked first, then the layers are drawn, then it is delivered")
    bal = db.balance(ws_row(MWS))
    V.next(packshot(), packshot())
    answers[:] = [dict(GOOD, other_text=["Everyday Carry Redefined"]), dict(GOOD, other_text=["Ready for anything"])]
    jid, price = make_offer()
    del order[:]
    tick_until_idle()
    j = job_row(jid)
    check(j["status"] == "failed" and j["error"] == image_qa.FAIL_TEXT and outputs_of(jid) == []
          and db.balance(ws_row(MWS)) == bal and "layers" not in order and len(V.submits[-1]["inputs"]["prompt"]) > 0,
          "invented copy twice: nothing is delivered, no layer is drawn on it, and every credit comes back")
    # with the switch off, the rest of the check does not judge a layered picture; with it on, it does
    V.next(packshot())
    answers[:] = [dict(GOOD, deformed_hands=True, label_matches=False, product_present=True)]
    jid, price = make_offer()
    tick_until_idle()
    with db.connect() as conn:
        qa = conn.execute("SELECT * FROM asset_qa WHERE job_id=? ORDER BY id", (jid,)).fetchall()
    check(job_row(jid)["status"] == "done" and len(qa) == 1 and qa[0]["verdict"] == "pass"
          and json.loads(qa[0]["checks_json"])["label_matches"] is False,
          "with IMAGE_QA off the label and hands answers are kept on the row but don't hold the picture back")
    # no vision key, or today's budget used: delivered unchecked, as the image check always does
    key = os.environ.pop("ANTHROPIC_API_KEY")
    V.next(packshot())
    jid, price = make_offer()
    n_calls = len(model_calls)
    tick_until_idle()
    with db.connect() as conn:
        qa = conn.execute("SELECT * FROM asset_qa WHERE job_id=? ORDER BY id", (jid,)).fetchall()
    os.environ["ANTHROPIC_API_KEY"] = key
    check(job_row(jid)["status"] == "done" and len(model_calls) == n_calls and [r["verdict"] for r in qa] == ["skipped"],
          "when the check can't run (no vision key) the picture is delivered unchecked: it never holds an ad hostage")

    print("== with IMAGE_QA on, a product-only picture with people in it fails the check")
    os.environ["IMAGE_QA"] = "1"
    check(image_qa.judge(dict(GOOD, people=True), no_people=True)["pass"] is False
          and image_qa.judge(dict(GOOD, people=True), no_people=True)["checks"]["no_people"] is False
          and image_qa.judge(dict(GOOD, people=True))["pass"] is True
          and image_qa.judge({k: v for k, v in GOOD.items() if k != "people"}, no_people=True)["pass"] is True
          and "people" in image_qa.SCHEMA["required"],
          "judge(): people in a product-only picture fail it; elsewhere, and when the model didn't say, they don't")
    # a hand at the edge the heuristic can't see: the vision check says "people", twice -> refunded
    bal, n_calls = db.balance(ws_row(MWS)), len(model_calls)
    answers[:] = [dict(GOOD, people=True), dict(GOOD, people=True)]
    V.next(packshot(), packshot())
    jid, price = make_offer()
    tick_until_idle()
    j = job_row(jid)
    with db.connect() as conn:
        qa = conn.execute("SELECT * FROM asset_qa WHERE job_id=? ORDER BY id", (jid,)).fetchall()
    check(j["status"] == "failed" and image_qa.FAIL_TEXT in j["error"] and db.balance(ws_row(MWS)) == bal
          and [r["verdict"] for r in qa] == ["fail", "fail"] and len(model_calls) == n_calls + 2 and outputs_of(jid) == []
          and "meant to show the product alone" in qa[0]["reasons_json"],
          "people twice in a product-only static: never delivered, credits returned")
    answers[:] = [dict(GOOD, people=True), GOOD]
    V.next(packshot(), packshot())
    jid, price = make_offer()
    tick_until_idle()
    check(job_row(jid)["status"] == "done" and len(outputs_of(jid)) == 1 and answers == [],
          "people once: made again silently, then delivered")
    answers[:] = [dict(GOOD, deformed_hands=True), GOOD]
    V.next(packshot(), packshot())
    jid, price = make_offer()
    tick_until_idle()
    check(job_row(jid)["status"] == "done" and answers == [] and len([s_ for s_ in V.submits[-2:]]) == 2,
          "and with the switch on the whole check judges a layered picture again (a malformed hand: made again)")
    # the heuristic's own retry spends no vision call on the first picture
    n_calls = len(model_calls)
    answers[:] = [GOOD]
    V.next(man_with_mug(), packshot())
    jid, price = make_offer()
    tick_until_idle()
    check(job_row(jid)["status"] == "done" and len(model_calls) == n_calls + 1 and answers == [],
          "the man: caught locally (no vision call), made again, and the second picture is checked as usual")
    answers[:] = [dict(GOOD, people=True)]
    V.next(man_with_mug())
    q = post(mem, "/app/create/quote", cfg).get_json()
    r = post(mem, "/app/create/generate", dict(cfg, total=q["total"])).get_json()
    tick_until_idle()
    with db.connect() as conn:
        pj = conn.execute("SELECT * FROM jobs WHERE batch_id=?", (r["batch"],)).fetchone()
    check(pj["status"] == "done" and answers == [], "an ordinary image ad with a person in it still passes the check")
    os.environ.pop("IMAGE_QA")
    textlayer.apply_to_file, llm._anthropic = _apply, _anth

    # ------------------------------------------------------------------ 4. the content filter and the softer prompt
    print("== a refused 'same person, somewhere new' still is tried once more with a softer prompt")
    akey = make_key(adm, "write")

    def lib(ids):
        q = call(akey, "quote_library_assets", {"kind": "presenter_stills", "ids": ids})["structuredContent"]
        g_ = call(akey, "generate_library_assets", {"kind": "presenter_stills", "ids": ids,
                                                     "confirm_total_credits": q["total_credits"]})["structuredContent"]
        return q["total_credits"], {j["key"]: j["job_id"] for j in g_["jobs"]}
    _c, first = lib(["cal:car"])
    tick_until_idle()
    check(job_row(first["cal:car"])["status"] == "done", "(Cal's first still is made)")
    soft = presenters.still_prompt("cal", "desk", same_person=True, soft=True)
    full = presenters.still_prompt("cal", "desk", same_person=True)
    cal = presenters.record("cal")
    check(soft.startswith(presenters.SAME_PERSON) and len(soft) < 0.62 * len(full) and cal["appearance"] not in soft
          and cal["appearance"] in full and presenters.situation("cal", "desk")["wardrobe"] not in soft
          and "wearing a plain " in soft and presenters.SITUATIONS["desk"]["place"] in soft
          and presenters.SITUATIONS["desk"]["scene"] not in soft and presenters.FICTIONAL in soft
          and "empty space where a product will stand" in soft,
          "the softer prompt: the same person from the reference, not described again; plain clothes with no "
          "adjectives; the place in one phrase")
    check(presenters.plain_wardrobe("a faded teal training top and black leggings") == "a plain top"
          and presenters.plain_wardrobe("an oatmeal-coloured knit sweater with the sleeves pushed up") == "a plain sweater"
          and presenters.plain_wardrobe("a soft lilac lounge set") == "plain everyday clothes"
          and presenters.plain_wardrobe("") == "plain everyday clothes"
          and all(presenters.plain_wardrobe(s["wardrobe"]).startswith(("a plain ", "plain everyday"))
                  for p in presenters.all_presenters() for s in p["situations"]),
          "plain_wardrobe(): the garment, no adjectives")
    for sid in presenters.situation_ids("cal"):
        if "portrait" in presenters.still_prompt("cal", sid, True, soft=True):
            check(False, "soft prompts are situation stills")
    check(presenters.portrait_prompt("cal", True, soft=True).startswith(presenters.SAME_PERSON)
          and presenters.still_prompt("cal", "desk", soft=True) == presenters.still_prompt("cal", "desk")
          and presenters.still_prompt("cal", "desk", True) == full,
          "a first still (no reference) has no softer version; the usual prompts are unchanged")
    bal = db.balance(ws_row(AWS))
    cost, jobs = lib(["cal:desk", "cal:shop-floor"])
    REASON = "Request blocked: content policy (category: apparel)"
    V.next({"status": "nsfw", "error": REASON}, {"status": "nsfw", "detail": {"message": "flagged"}},
           packshot((768, 1365)), {"status": "nsfw"})
    worker.tick()            # one pass: both sent, both refused, both back in the queue with the softer prompt
    check([s["inputs"]["prompt"] for s in V.submits[-2:]] == [full, presenters.still_prompt("cal", "shop-floor", True)],
          "(both go out with the full prompt first)")
    jd, js = job_row(jobs["cal:desk"]), job_row(jobs["cal:shop-floor"])
    check(jd["status"] == js["status"] == "pending" and jd["brief"] == soft and jd["error"] is None
          and params_of(jobs["cal:desk"])["softened"] is True and params_of(jobs["cal:desk"])["vendor_reason"] == REASON
          and params_of(jobs["cal:shop-floor"])["vendor_reason"] == "flagged" and jd["submit_key"] is None
          and db.balance(ws_row(AWS)) == bal - cost and jd["cost_usd"] == 0.215,
          "refused by the content filter: back in the queue with the softer prompt, nothing refunded yet, the "
          "vendor's own words kept")
    tick_until_idle()
    jd, js = job_row(jobs["cal:desk"]), job_row(jobs["cal:shop-floor"])
    resent = [s for s in V.submits if s["inputs"]["prompt"] == soft]
    check(jd["status"] == "done" and len(resent) == 1 and len(resent[0]["inputs"]["image_urls"]) == 1
          and resent[0]["key"] != V.submits[-4]["key"] and len(outputs_of(jobs["cal:desk"])) == 1,
          "the softer try is a new request with the same reference still, and this time it is made")
    check(js["status"] == "failed" and "content filter" in js["error"] and "Credits refunded" in js["error"]
          and db.balance(ws_row(AWS)) == bal - cost + js["credits"]
          and params_of(jobs["cal:shop-floor"])["vendor_reason"] == worker.NO_REASON,
          "one refused twice is given up and refunded; its last reason is kept (here the vendor gave none)")
    n_sub = len(V.submits)
    tick_until_idle()
    check(len(V.submits) == n_sub, "and it is not tried a third time")
    import html as _html
    page = _html.unescape(adm.get("/admin/library").get_data(as_text=True))
    check("Vendor said: " + REASON in page and "Made on the softer prompt after a content-filter refusal" in page
          and "Vendor said: " + worker.NO_REASON in page and "Tried again with the softer prompt" in page,
          "/admin/library shows the vendor's reason and which stills needed the softer prompt")
    lst = {a["job_id"]: a for a in call(akey, "list_library_assets", {"kind": "presenter_stills"})["structuredContent"]["assets"]}
    check(lst[jobs["cal:desk"]]["softened"] is True and lst[jobs["cal:desk"]]["vendor_reason"] == REASON
          and lst[jobs["cal:shop-floor"]]["status"] == "failed" and lst[first["cal:car"]]["vendor_reason"] is None
          and lst[first["cal:car"]]["softened"] is False, "and so does list_library_assets")
    # a first still (text to image) has no softer version: it fails at once, with the reason
    cost, f2 = lib(["bex:gym"])
    V.next({"status": "nsfw", "message": "blocked"})
    bal = db.balance(ws_row(AWS))
    tick_until_idle()
    check(job_row(f2["bex:gym"])["status"] == "failed" and params_of(f2["bex:gym"])["vendor_reason"] == "blocked"
          and "softened" not in params_of(f2["bex:gym"]) and db.balance(ws_row(AWS)) == bal + cost,
          "a refused first still fails at once and is refunded; the reason is kept")
    # a member's refused image ad: as before, and the vendor's words never reach them
    V.next({"status": "nsfw", "error": "internal moderation code 7"})
    bal = db.balance(ws_row(MWS))
    q = post(mem, "/app/create/quote", cfg).get_json()
    r = post(mem, "/app/create/generate", dict(cfg, total=q["total"])).get_json()
    n_sub = len(V.submits)
    tick_until_idle()
    with db.connect() as conn:
        mj = conn.execute("SELECT * FROM jobs WHERE batch_id=?", (r["batch"],)).fetchone()
    mpage = mem.get(f"/app/create/batch/{r['batch']}").get_data(as_text=True)
    check(mj["status"] == "failed" and mj["error"] == "Blocked by the content filter. Try a different brief or photo. Credits refunded."
          and len(V.submits) == n_sub + 1 and db.balance(ws_row(MWS)) == bal and "moderation code" not in mpage
          and "Blocked by the content filter" in mpage,
          "a member's refused ad fails and is refunded as before; they see our message, never the vendor's")
    V.next({"status": "failed", "error": {"message": "model overloaded"}})
    q = post(mem, "/app/create/quote", cfg).get_json()
    r = post(mem, "/app/create/generate", dict(cfg, total=q["total"])).get_json()
    tick_until_idle()
    with db.connect() as conn:
        mj = conn.execute("SELECT * FROM jobs WHERE batch_id=?", (r["batch"],)).fetchone()
    check(mj["status"] == "failed" and mj["error"].startswith("The generation failed.")
          and json.loads(mj["params_json"])["vendor_reason"] == "model overloaded",
          "a failed generation keeps the vendor's reason too")
    check(hf.refusal({"status": "nsfw"}) == "" and hf.refusal(None) == "" and hf.refusal({"error": "  a\n b "}) == "a b"
          and hf.refusal({"detail": {"reason": "x"}}) == "x" and hf.refusal({"moderation": ["a", "b"]}) == "a, b"
          and len(hf.refusal({"message": "z" * 900})) == 300 and hf.refusal({"error": True, "reason": "y"}) == "y",
          "hf.refusal(): the vendor's words from whichever field holds them, or nothing")

    # ------------------------------------------------------------------ 5. the bathroom-mirror wardrobe
    print("== the bathroom-mirror situation is dressed for an ad platform")
    bath = [(p["id"], s["wardrobe"]) for p in presenters.all_presenters() for s in p["situations"]
            if s["id"] == "bathroom-mirror"]
    check(len(bath) == 5 and {b[0] for b in bath} >= {"nia", "bex"}
          and all(re.search(r"crew-neck tee|robe closed high|zip hoodie zipped", w) for _i, w in bath)
          and not any(re.search(r"tank|vest|low|strap|crop|towel wrap|bra\b", w) for _i, w in bath),
          "every presenter with that situation wears a crew-neck tee, a closed robe or a zipped hoodie")
    dress = presenters.SITUATIONS["bathroom-mirror"]["dress"]
    check("high, round neckline" in dress and "covers the shoulders" in dress
          and not re.search(r"\b(no|not|never|without)\b", dress.lower())
          and all(dress in presenters.still_prompt(i, "bathroom-mirror", same) for i, _w in bath for same in (False, True))
          and all("dress" not in presenters.SITUATIONS[s] for s in presenters.SITUATIONS if s != "bathroom-mirror")
          and dress not in presenters.still_prompt("maya", "kitchen"),
          "its stills say so, as what to draw (never a list of what not to); no other situation changed")
    check(presenters.situation("maya", "kitchen")["wardrobe"] == "an oatmeal-coloured knit sweater with the sleeves pushed up"
          and presenters.situation("walt", "shop-floor")["wardrobe"] and len(presenters.all_presenters()) == 20
          and sum(len(p["situations"]) for p in presenters.all_presenters()) == 70,
          "the roster is otherwise what it was: 20 presenters, 70 situations")

    # ------------------------------------------------------------------ 6. an example's words are about its own product
    print("== every example's product matches its copy")
    import library

    def says(text, words_):
        return [w for w in words_ if re.search(r"\b" + re.escape(w) + r"s?\b", text, re.I)]
    images = [p for p in presets.LIBRARY if p.kind != "type" and p.tool == "ad_image"]
    for p in images:
        demo = library.demo_for(p)
        d = library.DEMO[demo]
        w = library.example_words(p.id)
        copy = json.dumps(w)
        wrong = [(o, says(copy, ws_)) for o, ws_ in library.DEMO_WORDS.items()
                 if o != demo and says(copy, [x for x in ws_ if x not in library.DEMO_WORDS[demo]])]
        badge = (w.get("layers") or {}).get("badge")
        fits = p.fits_product({"category": d["category"], "name": d["name"], "tags": d["tags"], "description": d["description"]})
        if wrong or (badge and not presets.badge_ok(badge, {"price": d["price"], "offer": d["offer"]})) or not fits \
                or (p.layout and p.id not in library._EXAMPLES):
            check(False, f"{p.id}: shown on {demo} but its words are about {wrong} (badge {badge!r}, fits {fits})")
    check(len(images) == 42 and set(library._EXAMPLES) == {p.id for p in presets.layered()}
          and all(e["demo"] in library.DEMO for e in library._EXAMPLES.values())
          and set(library.DEMO_WORDS) == set(library.DEMO),
          "all 42 image examples: each brief names its demo product and its words together; no example uses another "
          "product's words, a badge is that product's own saved price or offer, and the product fits the preset")
    tn = library._EXAMPLES["then-now"]
    check(library.demo_for(presets.get("then-now")) == "mug" and "Trail Mug" in tn["layers"]["right_title"]
          and "mug" in tn["headline"].lower() and says(json.dumps(tn), library.DEMO_WORDS["candle"]) == [],
          "then-now (the one that came back as a candle with the mug's words) is the mug, in every field")
    for pid, (demo, script) in library._SCRIPTS.items():
        short = library._SHORT_SCRIPTS.get(pid, "")
        for text in (script, short):
            other = [o for o, ws_ in library.DEMO_WORDS.items() if o != demo
                     and says(text, [x for x in ws_ if x not in library.DEMO_WORDS[demo]])]
            if other or (text and not says(text, list(library.DEMO_WORDS[demo]) + [library.DEMO_BRAND])):
                check(False, f"video example {pid} ({demo}): {text!r} is about {other or 'nothing of its own'}")
        if library.demo_for(presets.get(pid)) != demo or (script and pid not in library._SHORT_SCRIPTS):
            check(False, f"video example {pid}: one demo product, and a short line beside its script")
    check(True, "the video examples too: each script and its short line are about the product they are shown on")
    q0 = call(akey, "quote_library_assets", {"kind": "demo_products", "ids": ["mug", "candle"]})["structuredContent"]
    call(akey, "generate_library_assets", {"kind": "demo_products", "ids": ["mug", "candle"],
                                           "confirm_total_credits": q0["total_credits"]})
    tick_until_idle()
    res = call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": ["then-now"], "options": {"demo": "candle"}})
    ok_ = call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": ["shelf"], "options": {"demo": "candle"}})
    check(res["isError"] and "written for Dapplewood Trail Mug" in res["content"][0]["text"] and not ok_["isError"],
          "an example with words of its own can't be moved to another demo product; a scene with none can")
    answers[:] = [GOOD]
    q1 = call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": ["then-now"]})["structuredContent"]
    g1 = call(akey, "generate_library_assets", {"kind": "preset_examples", "ids": ["then-now"],
                                                "confirm_total_credits": q1["total_credits"]})["structuredContent"]
    tick_until_idle()
    tj = job_row(g1["jobs"][0]["job_id"])
    tp = json.loads(tj["params_json"])
    with db.connect() as conn:
        prod = conn.execute("SELECT name FROM products WHERE id=?", (tj["product_id"],)).fetchone()
    check(prod["name"] == "Dapplewood Trail Mug" and tp["product"] == "Dapplewood Trail Mug" and tp["library"]["demo"] == "mug"
          and tp["headline"] == "One mug, not a stack of cups" and tp["layers"]["right_title"] == "With Trail Mug"
          and tj["status"] == "done" and "Dapplewood Trail Mug standing in the upper half" in tj["brief"],
          "made for real: the then-now example's job is the mug, with the mug's headline, table and scene")

    print("== no vendor was really called")
    check(leaks == [], "no connection left this machine during the whole suite")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} m15 library fix checks passed")

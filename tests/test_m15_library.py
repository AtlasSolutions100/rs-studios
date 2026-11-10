"""The m15 library on a throwaway database: our own presets, the presenter roster, and the tools a site admin uses to
make and export their example assets.

  * presets: every record that existed before m15 is what it was (tests/fixtures/presets_m15_snapshot.json, taken
    from m15-integration before the library was added: prompts, prices, cards); at least 30 real ad presets, each a
    full record with a "what it's for" line and category + use tags; at least 10 fixed video formats with explicit
    beats of 6 to 8 seconds and an end card; the ones that need the keyframe-first pipeline are hidden from members
    and refused; seasonal statics; the house rules (no invented reviews, no countdowns, no customer voice)
  * presenters: at least 12 fictional presenters varied in age and gender, 3 or 4 situations each, one voice each;
    roster() / get() have exactly the shape the front end codes against and give a url only when the file is there;
    the eight older presenters and their prompts are unchanged
  * PRESENTER_STILLS and PRESENTER_VOICE_REF (both default off): off, a presenter video sends exactly what it sent
    before; on, the approved still goes first in image_urls and the voice sample goes in audio_urls
  * the library tools: quote first (credits and vendor USD), generate only with the quoted total, admin only (hidden
    from tools/list and refused for everyone else, 403 on the page); "same person, new situation" waits for the
    first still and passes it as the reference; every job is tagged; the export names files by their tags
  * tools/import_library_assets.py: tagged files become WebP of at most 200 KB (9:16 or 4:5) in the static layout
  * the claims check is free to members (0 credits), cached, and capped per day
  * MCP: list_ad_options returns presets with tags and examples and the roster; quote_ad_batch and the generate tools
    take a preset, a presenter and a situation; the old arguments still work

No network: every vendor is faked, and a socket that tries to leave the machine fails the test.

    python tests/test_m15_library.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_m15_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("FIRST_RUN_NUDGES", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "RS_FREE_PREVIEW", "RS_TALKING", "RS_SERVICE_PATH",
          "CLAIMS_CHECK", "CLAIMS_CHECK_DAILY_MAX", "CLAIMS_CHECK_WS_DAILY_MAX", "IMAGE_QA", "FREE_FIX", "TEXT_LAYERS",
          "RS_MARKUP", "RS_HD_MARKUP", "RS_TALKING_DURATIONS", "ANTHROPIC_FAST_MODEL", "LLM_USD_IN_PER_MTOK",
          "LLM_USD_OUT_PER_MTOK", "GROQ_API_KEY", "PRESENTER_STILLS", "PRESENTER_VOICE_REF", "RS_LIBRARY_STILL_MODEL",
          "RS_LIBRARY_STILL_USD", "RS_VIDEO_USD_PER_SECOND", "RS_TALKING_USD_PER_SECOND", "RS_VIDEO_USD_PER_SECOND_480P",
          "RS_CARD_REQUIRED", "UI_V2"):
    os.environ.pop(k, None)
# The keys are made-up strings: nothing here may reach a vendor with them (see the socket guard below).
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_PUBLIC_URL="https://campaignify.test", ANTHROPIC_API_KEY="sk-ant-test", RS_DISK_MIN_GB="0.2",
                  ADS_READY_EMAIL="0", MCP_SERVER="on")

# ---- the network guard: any connection that isn't to this machine fails the test (and is counted)
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
import claims_check  # noqa: E402
import compliance  # noqa: E402
import creator  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import library  # noqa: E402
import llm  # noqa: E402
import presenters  # noqa: E402
import presets  # noqa: E402
import products  # noqa: E402
import worker  # noqa: E402
from PIL import Image  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import import_library_assets  # noqa: E402
import presets_library_snapshot  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.31.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.environ_base["HTTP_USER_AGENT"] = UA
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def png_bytes(size=(768, 1365), color=(180, 170, 150)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


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


def make_key(c, scope="read"):
    r = c.post("/app/mcp", data={"csrf": csrf(c), "action": "create", "name": f"test {scope}", "scope": scope})
    m = re.search(r"(cfy_[A-Za-z0-9_-]{40,})", r.get_data(as_text=True))
    return m.group(1) if m else None


anon = appmod.app.test_client()
BASE = "http://localhost"          # what the MCP server calls itself under the test client
N = [0]


def rpc(method, params=None, key=None):
    N[0] += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": N[0]}
    if params is not None:
        msg["params"] = params
    return anon.post("/mcp", json=msg, headers={"Accept": "application/json, text/event-stream",
                                                 "Authorization": f"Bearer {key}"})


def call(key, name, args=None):
    r = rpc("tools/call", {"name": name, "arguments": args or {}}, key=key)
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json()["result"]


# ---------------------------------------------------------------- the fake vendors
class Vendor:
    """The generation vendor, in memory: submit returns a request id; status says completed with one picture (image
    models) or one clip (video models); uploads and the "download" of results are served from here too."""

    def __init__(self):
        self.submits, self.uploads, self.n, self.files, self.hold = [], [], 0, {}, False

    def upload(self, data, ctype):
        self.uploads.append({"bytes": len(data), "type": ctype})
        return f"https://files.example/up{len(self.uploads)}"

    def submit(self, model, inputs, webhook_url=None, idempotency_key=None):
        self.n += 1
        self.submits.append({"model": model, "inputs": inputs, "key": idempotency_key})
        return f"req-{self.n}"

    def status(self, rid):
        if self.hold:
            return {"status": "in_progress"}
        sub = self.submits[int(rid.split("-")[1]) - 1]
        if "video" in sub["model"]:
            url = f"https://cdn.example/{rid}.mp4"
            self.files[url] = b"\x00\x00\x00\x18ftypmp42" + b"0" * 4000
            return {"status": "completed", "video": {"url": url}}
        url = f"https://cdn.example/{rid}.png"
        self.files[url] = png_bytes()
        return {"status": "completed", "images": [{"url": url}]}


V = Vendor()
hf.upload, hf.submit, hf.status = V.upload, V.submit, V.status
estimates = {"value": None, "calls": []}


def _fake_estimate(model, inputs):
    estimates["calls"].append(model)
    return estimates["value"]


hf.estimate = _fake_estimate
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
worker.make_poster = lambda path: None            # no ffmpeg on a fake clip
model_calls, answers = [], []


# What the image check answers here: a clean picture. Since the paid runs a static with a text layer always has
# its text-free picture looked at (tests/test_m15_library_fixes.py covers what the check does with the answers).
CLEAN = {"product_present": True, "photo_has_label": True, "label_matches": True, "label_legible": True, "other_text": [],
         "garbled_text": False, "people": False, "deformed_hands": False, "deformed_faces": False, "notes": ""}


def _fake_anthropic(body, timeout=None):
    model_calls.append(body)
    if not answers and body["tools"][0]["name"] == "report":
        answers.append(dict(CLEAN))
    if not answers:
        raise AssertionError("an unexpected model call: " + body["tools"][0]["name"])
    ans = answers.pop(0)
    return {"model": body["model"], "usage": {"input_tokens": 1800, "output_tokens": 300},
            "content": [{"type": "tool_use", "name": body["tools"][0]["name"], "input": ans}]}


llm._anthropic = _fake_anthropic

# The library's files are read from static/: point every reader at a throwaway copy of that layout.
STATIC = os.path.join(TMP, "static")
os.makedirs(os.path.join(STATIC, "presenters"))
os.makedirs(os.path.join(STATIC, "presets"))
presenters.STATIC_DIR = os.path.join(STATIC, "presenters")
presets.EXAMPLE_DIR = os.path.join(STATIC, "presets")
worker.STATIC = STATIC
presenters.HERE = TMP              # library.py makes ref_static relative to <HERE>/static


def tick_until_idle(limit=12):
    for _ in range(limit):
        if not worker.tick():
            break


try:
    # ------------------------------------------------------------------ 1. nothing that existed has changed
    print("== the presets and presenters that existed before m15 are what they were")
    snap = json.load(open(os.path.join(HERE, "fixtures", "presets_m15_snapshot.json"), encoding="utf-8"))
    now = json.loads(json.dumps(presets_library_snapshot.snapshot()))
    check(len(snap["presets"]) == 39 and len(snap["presenters"]) == 8, "the fixture holds the 39 records and 8 presenters of before")
    for pid in presets_library_snapshot.BASE_IDS:
        if now["presets"][pid] != snap["presets"][pid]:
            diff = [k for k in snap["presets"][pid] if now["presets"][pid].get(k) != snap["presets"][pid][k]]
            check(False, f"preset {pid} changed: {diff}")
    check(True, "all 39 older presets: the same record, the same prompt sent to the vendor, the same price and card")
    for key in ("order", "presenters", "voices", "catalog_presenters", "tools", "talking_prices"):
        check(now[key] == snap[key], f"unchanged: {key}")
    check(set(catalog.TOOLS) == {"ad_image", "photo_to_video", "ugc_video", "motion_copy"}
          and "library_still" not in catalog.offered_tools() and catalog.INTERNAL_TOOLS["library_still"]["hidden"],
          "the library's still model is internal: not in TOOLS, never offered to members")

    # ------------------------------------------------------------------ 2. the preset library
    print("== the preset library")
    real = [p for p in presets.LIBRARY if p.kind != "type"]
    recipes = presets.RECIPES
    check(len(real) >= 30 and len(recipes) >= 10 and len(presets.STATICS_M15) >= 8,
          f"{len(real)} ad presets ({len(recipes)} fixed video formats, {len(presets.STATICS_M15)} new statics) "
          "besides the 5 ad types")
    codes = {c for c, _ in products.CATEGORIES} | {"any"}
    need = ("id", "kind", "name", "what_for", "format", "aspects", "inputs", "recipe", "model", "params", "beats",
            "text_layers", "qa", "locks", "price", "tags", "needs", "vendor_preset", "example", "example_video")
    for p in presets.LIBRARY:
        c = p.contract()
        ok = (not [k for k in need if k not in c] and c["what_for"] and c["inputs"] and c["recipe"] and c["model"]
              and c["beats"] and c["qa"] and c["price"]["credits"] > 0 and c["format"] in ("image", "video", "talking")
              and c["tags"]["use"] and set(c["tags"]["use"]) <= set(presets.USES)
              and c["tags"]["category"] and set(c["tags"]["category"]) <= codes and c["vendor_preset"] == "")
        if not ok:
            check(False, f"preset {p.id}: a full record with what_for and category + use tags ({c})")
    check(True, f"all {len(presets.LIBRARY)} records: the whole contract, a what_for line, category and use tags")
    uses = {u for p in presets.LIBRARY for u in p.use}
    check({"offer", "social-proof", "problem-solution", "unboxing-pov", "hands-only-demo", "talking-head-cutaways",
           "green-screen-product-page", "comparison", "marketplace", "seasonal", "founder-story", "how-to",
           "before-after-honest", "testimonial-real-only", "show-your-app", "street-interview", "listicle"} <= uses,
          "the use tags cover offer, social proof, problem-solution, unboxing, hands-only, cutaways, green screen, "
          "comparison, marketplace, seasonal, founder story, how-to, honest before/after and real-only testimonials")
    for r in recipes:
        spoken = [b for b in r.beats if b["beat"] != "end card"]
        ok = (r.kind == "recipe" and r.format == "talking" and r.tool == "ugc_video" and r.ad_type in ("ugc", "voiceover")
              and all(6 <= b["seconds"] <= 8 for b in spoken)
              and all({"beat", "seconds", "source", "shot", "say", "text"} <= set(b) for b in r.beats)
              and all(b["source"] in ("presenter", "product", "hands", "screen", "packshot") for b in r.beats)
              and sum(b["seconds"] for b in spoken) == r.price["duration"] in catalog.TOOLS["ugc_video"]["durations"]
              and r.extra["script"]["shape"] and r.needs in ("", "keyframe_video")
              and (len(spoken) == 1 or r.beats[-1]["beat"] == "end card")
              and r.price_credits() == catalog.price_credits("ugc_video", duration=r.price["duration"])
              and os.path.exists(os.path.join(ROOT, "static", "previews", r.preview + ".jpg"))
              and r.id in catalog.TEMPLATE_BY_ID)
        if not ok:
            check(False, f"recipe {r.id}: explicit beats of 6 to 8 s (shot, what is said, on-screen text), an end card, "
                         "a script shape and the talking-video price")
    check(True, f"the {len(recipes)} video recipes: beats of 6 to 8 seconds with shot, words, on-screen text and source, "
                "an end card, the table price")
    today = [r.id for r in recipes if presets.runnable(r)]
    later = [r.id for r in recipes if not presets.runnable(r)]
    check(len(today) >= 5 and len(later) >= 5 and set(later) >= {"talking-head-cutaways", "problem-solution",
                                                                 "green-screen-product-page", "show-your-app"}
          and [r.id for r in presets.recipes_for()] == today
          and not any(p.needs for p in presets.offered()) and len(presets.recipes_for(everything=True)) == len(recipes)
          and "keyframe_video" not in presets.PIPELINES,
          f"{len(today)} recipes run today as one take; the {len(later)} that need the keyframe-first pipeline are "
          "hidden from members")
    check(all(r.text_layers == () for r in recipes if not r.needs)
          and all("no on-screen text" in catalog.talking_prompt(r.ad_type, "Mug", "Hi.", "maya", "",
                                                                 direction=r.extra["direction"]).lower() for r in recipes),
          "a one-take recipe draws no words (the model never sets type)")
    seasonal = [p for p in presets.LIBRARY if "seasonal" in p.use and p.id != "gift"]
    check(len([p for p in seasonal if p.format == "image"]) >= 6 and any(p.format == "talking" for p in seasonal)
          and all(p.season for p in seasonal),
          f"{len(seasonal)} seasonal presets (statics and a clip), each with its season")
    brandk = {"name": "Northwind", "product": "Trail Mug"}
    for p in presets.STATICS_M15:
        brief = catalog.fill(p.recipe, brandk)
        scene = catalog.scene_only(catalog.split_image_brief(brief)["scene"])
        ok = (compliance.check(brief)["ok"] and "Trail Mug" in scene and "{" not in brief and len(scene) > 0.8 * len(brief)
              and p.layout in presets.ZONES and p.id in catalog.TEMPLATE_BY_ID
              and os.path.exists(os.path.join(ROOT, "static", "previews", p.preview + ".jpg")))
        if not ok:
            check(False, f"new static {p.id}: a rule-clean brief that survives the scene filter, a layout and a thumbnail")
    check(len({p.recipe for p in presets.LAYERED + presets.STATICS_M15}) == len(presets.LAYERED + presets.STATICS_M15),
          "every new static has its own scene")
    words = " ".join((p.what_for + " " + p.recipe + " " + json.dumps(p.extra.get("script") or {}) + " "
                      + " ".join(b.get("say", "") + " " + b.get("text", "") for b in p.beats))
                     for p in presets.STATICS_M15 + recipes).lower()
    check(not re.search(r"hurry|ends tonight\b(?! unless)|only \d+ left|while supplies last|limited time", words.replace(
        "no 'ends tonight' unless", "")) and "countdown" in words and "never" in words,
          "no recipe writes urgency: countdowns and made-up deadlines are named only to rule them out")
    for r in recipes:
        starter = r.extra["script"]["starter"].replace("{product}", "Trail Mug")
        chk = compliance.check(starter)
        if not chk["ok"] or creator.CUSTOMER_VOICE in chk["warnings"]:
            check(False, f"recipe {r.id}: its starter line breaks a rule ({chk})")
    check(True, "no recipe's starter line speaks as a customer or breaks a rule")
    tn = presets.get("then-now")
    check(tn.fits == ("home", "apparel", "app_service", "other") and not tn.fits_product({"name": "Face serum", "category": "beauty"})
          and tn.fits_product({"name": "Mug", "category": "home"}) and presets.get("review").layout == "review"
          and presets.layout_spec(presets.get("review"))["ai_items"] is False,
          "the honest before/after is never offered for beauty, supplements or food; the review card is still real-only")
    try:
        presets.clean_layers(tn, {"left": ["Cold by ten"], "right": ["Still warm"]}, {"product": "Trail Mug"})
        got = ""
    except presets.LayerError as e:
        got = str(e)
    try:
        presets.clean_layers(tn, {"left": ["Before: dull. After: glowing."], "right": ["x"], "true": True}, {"product": "M"})
        ba = ""
    except presets.LayerError as e:
        ba = str(e)
    ok_rows = presets.clean_layers(tn, {"left": ["A paper cup"], "right": ["A steel mug"], "true": True}, {"product": "Trail Mug"})
    check("confirm both columns are true" in got and ok_rows["true"] is True and ok_rows["left_title"] == "The old way"
          and "before-and-after" in ba and not compliance.check(tn.name + " " + tn.what_for)["blocks"],
          "the honest switch needs the member's word that both columns are true, and the house rule against "
          "before-and-after wording still applies to every line")
    pub = presets.public(presets.get("car-talk"))
    check(pub["tags"] == {"category": ["any"], "use": ["talking-head"]} and pub["situation"] == "car" and pub["runnable"]
          and pub["example"] is None and pub["beats"] and pub["duration"] == 8 and pub["from"] == 740
          and presets.public(presets.get("offer"))["fields"][0]["id"] == "badge",
          "presets.public(): a preset's card plus its tags, example urls and recipe details")
    check(presets.example_url("offer") is None and presets.example_video_url("car-talk") is None
          and presets.example_url("../etc") is None,
          "a preset's example url is None until its file is imported")
    for prod in ({"name": "Thing", "category": "home"}, {"name": "App", "category": "app_service"},
                 {"name": "Serum", "category": "beauty"}):
        ids = [p.id for p in presets.library_for(prod)]
        check(len(ids) == len(set(ids)) and ("then-now" in ids) == (prod["category"] != "beauty")
              and ("app-screen" in ids) == (prod["category"] == "app_service") and "season-autumn" in ids
              and not any(presets.get(i).kind == "recipe" for i in ids),
              f"Create's list for {prod['category']}: the new statics that fit it, no video recipe (the page can't send one yet)")

    # ------------------------------------------------------------------ 3. the presenter roster
    print("== the presenter roster")
    ro = presenters.roster()
    check(len(ro) >= 12 and len({p["id"] for p in ro}) == len(ro) == len({p["name"] for p in ro}),
          f"{len(ro)} presenters, each with its own id and first name")
    keys = {"id", "name", "age_band", "gender", "look", "voice", "portrait", "voice_sample", "situations"}
    for p in ro:
        rec = presenters.record(p["id"])
        ok = (keys <= set(p) and set(p["voice"]) == {"id", "label"} and p["voice"]["label"]
              and p["voice"]["id"] in catalog.VOICE_BY_ID and catalog.default_voice(p["id"]) == p["voice"]["id"]
              and 3 <= len(p["situations"]) <= 4 and all(set(s) == {"id", "label", "still"} for s in p["situations"])
              and all(s["id"] in presenters.SITUATIONS for s in p["situations"])
              and p["age_band"] in presenters.AGE_BANDS and p["gender"] in ("female", "male", "nonbinary")
              and p["portrait"] is None and p["voice_sample"] is None and all(s["still"] is None for s in p["situations"])
              and re.fullmatch(r"[a-z]+", p["id"]) and len(rec["appearance"]) > 150
              and all(s["wardrobe"] for s in rec["situations"])
              and not re.search(r"looks? like|resembl|celebrit|famous|actor|actress|lookalike", rec["appearance"] + rec["look"], re.I)
              and presenters.get(p["id"]) == p)
        if not ok:
            check(False, f"presenter {p['id']}: the roster's shape, 3 or 4 situations with a wardrobe, one known voice, "
                         "a long description that names nobody")
    check(True, "every presenter: id, name, age band, gender, look, voice {id, label}, portrait, voice_sample, "
                "situations [{id, label, still}]; urls are None while no file exists")
    check(set(presenters.SITUATIONS) == {"kitchen", "car", "outdoor", "desk", "sofa", "bathroom-mirror", "gym", "shop-floor"}
          and {s["id"] for p in ro for s in p["situations"]} == set(presenters.SITUATIONS)
          and len({p["age_band"] for p in ro}) >= 5 and {p["gender"] for p in ro} >= {"female", "male"}
          and sum(p["gender"] == "male" for p in ro) >= 5 and sum(p["gender"] == "female" for p in ro) >= 5
          and presenters.get("nobody") is None and len({p["voice"]["id"] for p in ro}) >= 12,
          "eight situations, all used; five age bands or more; men and women; at least twelve distinct voices")
    check(catalog.PRESENTERS == snap["catalog_presenters"] and len(catalog.PRESENTERS) == 8
          and set(catalog.PRESENTER_BY_ID) == set(presenters.IDS) == set(catalog.PRESENTER_VOICE)
          and [p["id"] for p in ro][:8] == [p["id"] for p in catalog.PRESENTERS]
          and len(catalog.VOICES) == 6 and catalog.default_voice("nobody") == catalog.VOICES[0]["id"],
          "the eight older presenters lead the roster unchanged; every id resolves in catalog; the six pickable voices stay")
    sp = presenters.still_prompt("walt", "shop-floor")
    sp2 = presenters.still_prompt("walt", "car", same_person=True)
    check(all(x in sp for x in ("front camera of an ordinary phone", "visible pores", "no studio lighting",
                                "mouth closed and relaxed", "clear, empty space", "where a product will stand",
                                "does not resemble any real or famous individual", "canvas work apron"))
          and "reference image" not in sp and sp2.startswith("The reference image shows this person.")
          and "exactly the same person" in sp2 and "tan field jacket" in sp2 and "parked, ordinary car" in sp2,
          "a still's prompt: an ordinary room, phone framing, natural skin, no studio light, a relaxed mouth, room for a "
          "product; the second path keeps the same person and changes the place and clothes")

    # ------------------------------------------------------------------ an admin, a member, a product each
    adm, au = signup("owner@example.com", credits=100000, admin=True, plan="scale")
    mem, mu = signup("member@example.com", credits=5000)
    AWS, MWS = au["workspace_id"], mu["workspace_id"]
    photo_path = os.path.join(TMP, "product.png")
    open(photo_path, "wb").write(png_bytes((600, 600), (90, 110, 130)))

    def add_product(user, ws_id, brand_name, name, claims):
        with appmod.app.test_request_context("/"):
            from flask import g
            with db.connect() as conn:
                g.user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
                pid = products.save_kit(conn, g.ws, brand_name=brand_name, product_name=name, audience="Hikers", claims=claims)
                ph = products.add_photo(conn, ws_id, pid, photo_path, "product.png", "image/png")
                conn.execute("UPDATE products SET price='$24', offer='Free lid with every mug' WHERE id=?", (pid,))
                conn.commit()
        return pid, ph
    PID, PH = add_product(mu, MWS, "Northwind", "Trail Mug", "Keeps drinks hot for 6 hours")
    APID, _ = add_product(au, AWS, "House", "House Mug", "")

    # ------------------------------------------------------------------ 4. Create: a preset, a presenter, a situation
    print("== Create takes a video preset, any roster presenter and a situation")
    old = {"product": PID, "type": "ugc", "items": ["Meet the mug."], "body": "Try it today.", "presenters": ["maya"],
           "duration": 8, "aspects": ["9:16"], "photos": [PH]}
    q0 = post(mem, "/app/create/quote", old).get_json()
    check(q0["count"] == 1 and q0["total"] == catalog.price_credits("ugc_video", duration=8)
          and q0["lines"][0]["label"] == "Maya · 9:16", "the old arguments price as before")
    newc = dict(old, presenters=["walt"], situation="shop-floor", preset="talk-to-camera")
    q1 = post(mem, "/app/create/quote", newc).get_json()
    check(q1["total"] == q0["total"] and q1["lines"][0]["label"] == "Walt · Shop floor · 9:16",
          "a new presenter in a situation, on a video preset: the same table price")
    for bad, why in ((dict(newc, preset="problem-solution"), "isn't available yet"),
                     (dict(newc, preset="vo-product-shots"), "voiceover preset"),
                     (dict(newc, preset="offer"), "isn't a video preset"),
                     (dict(newc, situation="the moon"), "one of the places"),
                     (dict(newc, presenters=["nobody"]), "at least one presenter")):
        r = post(mem, "/app/create/quote", bad).get_json()
        check(why in (r.get("error") or ""), f"refused: {why}")
    r = post(mem, "/app/create/generate", dict(newc, total=q1["total"]))
    jw = db.connect().execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (MWS,)).fetchone()
    pw = json.loads(jw["params_json"])
    check(r.status_code == 200 and pw["presenter"] == "walt" and pw["situation"] == "shop-floor"
          and pw["preset"] == "talk-to-camera" and pw["voice"] == "steady_m60" and jw["template_id"] == "talk-to-camera"
          and "library" not in pw, "the job carries the preset, the presenter, their voice and the situation")
    r = post(mem, "/app/create/generate", dict(old, total=q0["total"]))
    jo = db.connect().execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (MWS,)).fetchone()
    po = json.loads(jo["params_json"])
    check(r.status_code == 200 and "situation" not in po and "preset" not in po and po["voice"] == "warm_f",
          "an old-style job carries nothing new")
    car = dict(old, preset="car-talk", situation="kitchen")
    post(mem, "/app/create/generate", dict(car, total=q0["total"]))
    jc = db.connect().execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (MWS,)).fetchone()
    check(json.loads(jc["params_json"])["situation"] == "car", "a preset that fixes its place (car talk) keeps it")

    # ------------------------------------------------------------------ 5. PRESENTER_STILLS / PRESENTER_VOICE_REF
    print("== the presenter's still and voice sample, behind their switches")
    check(presenters.stills_on() is False and presenters.voice_ref_on() is False, "both switches are off by default")
    with db.connect() as conn:
        before = worker.build_inputs(conn, jo)
        check(before == {"prompt": catalog.talking_prompt("ugc", "Trail Mug", "Meet the mug. Try it today.", "maya", "warm_f"),
                         "image_urls": before["image_urls"], "duration": 8, "aspect_ratio": "9:16", "resolution": "720p",
                         "bitrate_mode": "high", "generate_audio": True} and len(before["image_urls"]) == 1
              and before["prompt"] == snap["presenters"]["maya"]["table"].replace(
                  presets_library_snapshot.SCRIPT, "Meet the mug. Try it today."),
              "off: an old-style presenter video sends exactly the old prompt and only the product photo")
        w0 = worker.build_inputs(conn, jw)
        check(len(w0["image_urls"]) == 1 and "audio_urls" not in w0 and "first reference image" not in w0["prompt"]
              and "small independent shop" in w0["prompt"] and "wooden counter in front of them" in w0["prompt"]
              and "steady, slightly gravelly older male voice" in w0["prompt"],
              "off: a situation only changes the words (the place, where the product stands); no still, no audio")
        os.makedirs(os.path.join(STATIC, "presenters", "walt"))
        open(os.path.join(STATIC, "presenters", "walt", "shop-floor.webp"), "wb").write(b"RIFFxxxxWEBP" + b"1" * 300)
        open(os.path.join(STATIC, "presenters", "walt", "voice.mp3"), "wb").write(b"ID3" + b"2" * 300)
        check(worker.build_inputs(conn, jw) == w0, "off, with the files there: still exactly the same")
        os.environ["PRESENTER_STILLS"] = "1"
        n_up = len(V.uploads)
        w1 = worker.build_inputs(conn, jw)
        check(len(w1["image_urls"]) == 2 and w1["image_urls"][1:] == w0["image_urls"] and V.uploads[-1]["type"] == "image/webp"
              and "The presenter is the person in the first reference image" in w1["prompt"]
              and "every reference image after the first" in w1["prompt"] and "audio_urls" not in w1,
              "PRESENTER_STILLS on: the approved still is the first reference image and the prompt says to keep that person")
        check(worker.build_inputs(conn, jw)["image_urls"][0] == w1["image_urls"][0] and len(V.uploads) == n_up + 1,
              "the still is uploaded once, then its link is reused")
        check(worker.build_inputs(conn, jo) == before, "on, for a presenter with no still: exactly as before")
        os.makedirs(os.path.join(STATIC, "presenters", "maya"))
        open(os.path.join(STATIC, "presenters", "maya", "kitchen.webp"), "wb").write(b"RIFFxxxxWEBP" + b"3" * 300)
        d1 = worker.build_inputs(conn, jo)
        conn.execute("UPDATE jobs SET params_json=? WHERE id=?", (json.dumps(dict(po, setting="a quiet porch")), jo["id"]))
        d2 = worker.build_inputs(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (jo["id"],)).fetchone())
        conn.rollback()
        shutil.rmtree(os.path.join(STATIC, "presenters", "maya"))
        check(len(d1["image_urls"]) == 2 and "first reference image" in d1["prompt"] and "kitchen counter beside them" in d1["prompt"]
              and len(d2["image_urls"]) == 1 and "a quiet porch" in d2["prompt"] and "first reference image" not in d2["prompt"],
              "on, with no place picked (the older pages): the presenter's first situation's still is used; a typed "
              "setting keeps the old behaviour")
        os.environ["PRESENTER_VOICE_REF"] = "1"
        w2 = worker.build_inputs(conn, jw)
        check(len(w2.get("audio_urls") or []) == 1 and V.uploads[-1]["type"] == "audio/mpeg"
              and "the voice heard in audio 1" in w2["prompt"] and "never repeat its words" in w2["prompt"],
              "PRESENTER_VOICE_REF on: the voice sample goes in audio_urls and the prompt says to speak in that voice")
        conn.execute("UPDATE jobs SET params_json=? WHERE id=?", (json.dumps(dict(pw, voice="calm_m")), jw["id"]))
        other = worker.build_inputs(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (jw["id"],)).fetchone())
        check("audio_urls" not in other and "audio 1" not in other["prompt"],
              "a member who picked another voice doesn't get the presenter's sample")
        conn.rollback()
        os.environ.pop("PRESENTER_STILLS")
        os.environ.pop("PRESENTER_VOICE_REF")
        check(worker.build_inputs(conn, jw) == w0, "switched off again: back to before")
    rr = presenters.get("walt")
    check(rr["voice_sample"] == "/static/presenters/walt/voice.mp3" and rr["portrait"] is None
          and [s["still"] for s in rr["situations"]] == [None, "/static/presenters/walt/shop-floor.webp", None, None]
          and presenters.get("nia")["situations"][0]["still"] is None,
          "roster(): a url appears for exactly the files that exist")
    shutil.rmtree(os.path.join(STATIC, "presenters", "walt"))
    with db.connect() as conn:            # the member's queued videos are not rendered in this suite
        for j in conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND status='pending'", (MWS,)).fetchall():
            worker._fail(conn, j, worker.CANCELED)

    # ------------------------------------------------------------------ 6. the library tools: admin only, price first
    print("== library assets: quote, then generate with the quoted total; admins only")
    akey, mkey = make_key(adm, "write"), make_key(mem, "write")
    names = lambda key: {t["name"] for t in rpc("tools/list", {}, key=key).get_json()["result"]["tools"]}  # noqa: E731
    LIB = {"quote_library_assets", "generate_library_assets", "list_library_assets"}
    check(LIB <= names(akey) and not (LIB & names(mkey)), "tools/list shows the three library tools to a site admin only")
    for t, a in (("quote_library_assets", {"kind": "presenter_stills", "ids": ["maya"]}),
                 ("generate_library_assets", {"kind": "presenter_stills", "ids": ["maya"], "confirm_total_credits": 1}),
                 ("list_library_assets", {})):
        res = call(mkey, t, a)
        check(res["isError"] and "Unknown tool" in res["content"][0]["text"], f"{t}: refused for a member")
    with appmod.app.test_request_context("/"):
        class _Ctx:
            user_id, account_id = mu["id"], MWS
        import mcp_tools
        try:
            mcp_tools.library_quote(_Ctx(), {"kind": "presenter_stills", "ids": ["maya"]})
            got = ""
        except mcp_tools.ToolError as e:
            got = str(e)
    check("own admins" in got, "and the tool's own code refuses a non-admin even if it were reached")
    check(mem.get("/admin/library").status_code == 403 and mem.get("/admin/library/export.zip").status_code == 403
          and client().get("/admin/library").status_code == 302, "the page is 403 for a member and asks a visitor to log in")

    estimates["value"] = 0.25
    q = call(akey, "quote_library_assets", {"kind": "presenter_stills", "ids": ["maya", "nia:desk"]})["structuredContent"]
    check(q["count"] == 4 and [ln["key"] for ln in q["lines"]] == ["maya:kitchen", "maya:sofa", "maya:car", "nia:desk"]
          and q["total_credits"] == 4 * 50 and q["vendor_usd"] == 1.0 and "live estimate" in q["vendor_usd_basis"]
          and q["lines"][1]["starts_from"] == "maya:kitchen" and q["lines"][0]["starts_from"] is None
          and "confirm_total_credits=200" in q["next"] and estimates["calls"]
          and set(estimates["calls"]) == {"marketing-studio/image/sunburst"},
          "a quote: total credits, the vendor's own (free) estimate in USD, one line per still, and what each starts from")
    n_est = len(estimates["calls"])
    call(akey, "quote_library_assets", {"kind": "presenter_stills", "ids": ["maya"]})
    check(len(estimates["calls"]) == n_est, "the vendor's estimate is asked once per setting and kept for 15 minutes")
    with db.connect() as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'library_estimate:%'")
        conn.commit()
    estimates["value"] = None
    args = {"kind": "presenter_stills", "ids": ["maya", "nia:desk", "nia:sofa"]}
    q = call(akey, "quote_library_assets", args)["structuredContent"]
    first, rest = catalog.library_still_usd(), catalog.library_still_usd(refs=1)
    check(q["count"] == 5 and q["vendor_usd"] == round(2 * first + 3 * rest, 2) and "our price table" in q["vendor_usd_basis"]
          and q["total_credits"] == 2 * 40 + 3 * 43 and q["credits_available"] == 100000 and q["short_by"] == 0
          and first == 0.20 and rest == 0.215,
          "with no vendor estimate the quote uses our table: $0.20 a first still, $0.215 with its reference, at 2x")
    bal0, n_jobs = db.balance(ws_row(AWS)), db.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    for wrong in (q["total_credits"] - 1, 0):
        res = call(akey, "generate_library_assets", dict(args, confirm_total_credits=wrong))
        check(res["isError"] and "Quote it again" in res["content"][0]["text"], f"a total of {wrong} is refused")
    check(db.balance(ws_row(AWS)) == bal0 and db.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == n_jobs
          and not V.submits, "a wrong total starts nothing and charges nothing")
    res = call(akey, "generate_library_assets", {"kind": "presenter_stills", "ids": ["nia:sofa"], "confirm_total_credits": 43})
    check(res["isError"] and "first still (desk)" in res["content"][0]["text"],
          "a second situation with no first still to start from is refused")
    res = call(mkey, "quote_ad_batch", {"ad_type": "image", "product_id": PID, "lines": ["Hi"]})
    check(not res["isError"], "(the member's own tools are untouched)")
    g1 = call(akey, "generate_library_assets", dict(args, confirm_total_credits=q["total_credits"]))["structuredContent"]
    jobs = {j["key"]: j["job_id"] for j in g1["jobs"]}
    check(g1["queued"] == 5 and g1["credits_charged"] == q["total_credits"] == bal0 - db.balance(ws_row(AWS))
          and g1["not_started"] == [], "the quoted total starts all five and takes exactly that many credits")
    pk, ps, pd = params_of(jobs["maya:kitchen"]), params_of(jobs["maya:sofa"]), params_of(jobs["nia:sofa"])
    check(pk["library"] == {"kind": "presenter_still", "presenter": "maya", "situation": "kitchen"}
          and "ref_job" not in pk and ps["ref_job"] == jobs["maya:kitchen"] and pd["ref_job"] == jobs["nia:desk"]
          and ps["library"]["situation"] == "sofa" and pk["quality"] == "high" and pk["resolution"] == "1k"
          and job_row(jobs["maya:kitchen"])["tool"] == "library_still" and job_row(jobs["maya:sofa"])["workspace_id"] == AWS
          and job_row(jobs["maya:sofa"])["cost_usd"] == 0.215,
          "every job is tagged params.library = {kind, presenter, situation}; the later stills name the first as their reference")
    V.hold = True
    worker.tick()
    check([s["model"] for s in V.submits] == ["marketing-studio/image/sunburst"] * 2
          and all("image_urls" not in s["inputs"] for s in V.submits)
          and V.submits[0]["inputs"] == {"prompt": presenters.still_prompt("maya", "kitchen"), "resolution": "1k",
                                         "aspect_ratio": "9:16", "quality": "high", "enhance_prompt": False}
          and job_row(jobs["maya:sofa"])["status"] == "pending" and job_row(jobs["maya:sofa"])["attempts"] == 0,
          "the worker starts only the two first stills (text to image); the others wait for them")
    worker.tick()
    check(len(V.submits) == 2, "and keep waiting while the first stills are being made")
    V.hold = False
    n_up = len(V.uploads)
    tick_until_idle()
    later_subs = V.submits[2:]
    check(len(later_subs) == 3 and all(len(s["inputs"]["image_urls"]) == 1 for s in later_subs)
          and len(V.uploads) == n_up + 3 and later_subs[0]["inputs"]["prompt"] == presenters.still_prompt("maya", "sofa", True)
          and all(job_row(j)["status"] == "done" for j in jobs.values()),
          "once a first still is done, the same person's other situations are made from it as the reference image")
    lst = call(akey, "list_library_assets", {"kind": "presenter_stills"})["structuredContent"]
    a0 = next(a for a in lst["assets"] if a["job_id"] == jobs["nia:sofa"])
    check(lst["total"] == 5 and a0["presenter"] == "nia" and a0["situation"] == "sofa" and a0["status"] == "done"
          and a0["outputs"][0]["filename"] == "presenter__nia__sofa.png"
          and a0["outputs"][0]["download_url"].startswith(BASE + "/mcp/file/")
          and anon.get(a0["outputs"][0]["download_url"].replace(BASE, "")).status_code == 200,
          "list_library_assets: asset ids, who and where, status, the tag file name and a working download link")
    # a failed first still: the ones that start from it are refunded, never sent
    estimates["value"] = None
    qf = call(akey, "quote_library_assets", {"kind": "presenter_stills", "ids": ["otis"]})["structuredContent"]
    gf = call(akey, "generate_library_assets", {"kind": "presenter_stills", "ids": ["otis"],
                                                "confirm_total_credits": qf["total_credits"]})["structuredContent"]
    fj = {j["key"]: j["job_id"] for j in gf["jobs"]}
    balf = db.balance(ws_row(AWS))
    with db.connect() as conn:
        worker._fail(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (fj["otis:gym"],)).fetchone(), "The generation failed.")
    n_sub = len(V.submits)
    tick_until_idle()
    check(len(V.submits) == n_sub and all(job_row(j)["status"] == "failed" for j in fj.values())
          and db.balance(ws_row(AWS)) == balf + qf["total_credits"]
          and "wasn't started" in job_row(fj["otis:car"])["error"],
          "when a first still fails, the stills that needed it are not sent and every credit comes back")
    # the vendor's price moved: the still is refused and refunded
    estimates["value"] = None
    qx = call(akey, "quote_library_assets", {"kind": "presenter_stills", "ids": ["june:kitchen"]})["structuredContent"]
    gx = call(akey, "generate_library_assets", {"kind": "presenter_stills", "ids": ["june:kitchen"],
                                                "confirm_total_credits": qx["total_credits"]})["structuredContent"]
    estimates["value"], n_sub, balx = 0.60, len(V.submits), db.balance(ws_row(AWS))
    tick_until_idle()
    check(len(V.submits) == n_sub and job_row(gx["jobs"][0]["job_id"])["status"] == "failed"
          and db.balance(ws_row(AWS)) == balx + qx["total_credits"],
          "a still whose live vendor estimate is far over the quote is refused before it is sent, and refunded")
    estimates["value"] = None

    # ------------------------------------------------------------------ 7. demo products, preset examples, clips
    print("== the demo brand, preset examples and video examples")
    for did, d in library.DEMO.items():
        text = " ".join(str(d[k]) for k in ("name", "description", "offer", "claims"))
        if not compliance.check(text)["ok"] or d["category"] not in dict(products.CATEGORIES) or not library.packshot_prompt(did):
            check(False, f"demo product {did}: rule-clean details, a known category and a packshot prompt")
    check(len(library.DEMO) >= 5 and {"mug", "candle", "skincare", "coffee", "app"} <= set(library.DEMO)
          and all(library.DEMO_BRAND.upper() in library.packshot_prompt(d) or library.DEMO_BRAND in library.packshot_prompt(d)
                  for d in library.DEMO) and "No props" in library.packshot_prompt("mug")
          and "no device frame" in library.packshot_prompt("app"),
          f"{len(library.DEMO)} demo products of one fictional brand, each with a packshot prompt that prints only its own label")
    res = call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": ["offer"]})
    check(res["isError"] and "no packshot yet" in res["content"][0]["text"], "a preset example needs the demo packshot first")
    qd = call(akey, "quote_library_assets", {"kind": "demo_products", "ids": ["all"]})["structuredContent"]
    check(qd["count"] == len(library.DEMO) and qd["total_credits"] == 40 * len(library.DEMO), "the demo packshots are priced")
    gd = call(akey, "generate_library_assets", {"kind": "demo_products", "ids": ["all"],
                                                "confirm_total_credits": qd["total_credits"]})["structuredContent"]
    tick_until_idle()
    dj = {j["key"]: j["job_id"] for j in gd["jobs"]}
    sub = next(s for s in V.submits if s["inputs"]["prompt"] == library.packshot_prompt("mug"))
    check(all(job_row(j)["status"] == "done" for j in dj.values()) and params_of(dj["app"])["aspect"] == "9:16"
          and params_of(dj["mug"])["library"] == {"kind": "demo_product", "demo": "mug"} and "image_urls" not in sub["inputs"],
          "the packshots are made text to image, tagged with their demo product")
    images = [p.id for p in presets.LIBRARY if p.kind != "type" and p.tool == "ad_image"]
    qe = call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": ["all"]})["structuredContent"]
    check(qe["count"] == len(images) and qe["total_credits"] == len(images) * catalog.price_credits("ad_image", batch=1)
          and abs(qe["vendor_usd"] - round(len(images) * 0.1071, 2)) < 0.011,
          f"every image preset ({len(images)}) can be priced on its demo product at an image ad's price")
    def demo_rows():
        with db.connect() as conn:
            return conn.execute("SELECT p.name, p.category, (SELECT COUNT(*) FROM photos ph WHERE ph.product_id=p.id) n "
                                "FROM products p JOIN brands b ON b.id=p.brand_id WHERE p.workspace_id=? AND b.brand_name=?",
                                (AWS, library.DEMO_BRAND)).fetchall()
    check(demo_rows() == [], "a quote writes nothing: the demo products aren't in the workspace yet")
    pick = ["offer", "review", "then-now", "season-autumn", "shelf", "iced", "app-screen", "marketplace", "outfit",
            "counter", "vanity"]
    qe = call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": pick})["structuredContent"]
    ge = call(akey, "generate_library_assets", {"kind": "preset_examples", "ids": pick,
                                                "confirm_total_credits": qe["total_credits"]})["structuredContent"]
    rows_ = demo_rows()
    check(len(rows_) == len(library.DEMO) - 1 and all(r["n"] == 1 for r in rows_)
          and {r["category"] for r in rows_} >= {"home", "beauty", "food_drink", "apparel", "app_service"},
          "generating set the demo products it needed up as real products of the admin's workspace, each with its "
          "packshot as its photo")
    check(call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": pick})["structuredContent"]["total_credits"]
          == qe["total_credits"], "and the quote is the same before and after they exist")
    ej = {j["key"]: j["job_id"] for j in ge["jobs"]}
    po_, pr_, pi_ = params_of(ej["offer"]), params_of(ej["review"]), params_of(ej["iced"])
    check(ge["queued"] == len(pick) and po_["library"] == {"kind": "preset_example", "preset": "offer", "demo": "mug"}
          and po_["layout"] == "offer" and po_["layers"]["badge"] == "$24" and job_row(ej["offer"])["template_id"] == "offer"
          and "goes here" in pr_["headline"] and pr_["layers"]["stars"] == 0
          and pi_["library"]["demo"] == "coffee" and params_of(ej["app-screen"])["library"]["demo"] == "app"
          and params_of(ej["season-autumn"])["library"]["demo"] == "candle" and job_row(ej["shelf"])["tool"] == "ad_image",
          "examples run as the preset itself runs (an image ad on a demo product), tagged with the preset; the review "
          "card's example is a placeholder that says so, never an invented review")
    tick_until_idle()
    check(all(job_row(j)["status"] == "done" for j in ej.values()), "and they finish like any image ad")
    for k, (demo, script) in library._SCRIPTS.items():
        pre = presets.get(k)
        if script:
            chk = compliance.check(script)
            if not chk["ok"] or creator.CUSTOMER_VOICE in chk["warnings"] or \
                    len(script.split()) > catalog.hard_words(pre.price["duration"]):
                check(False, f"example script for {k}: rule-clean, not a customer's voice, and short enough")
    res = call(akey, "quote_library_assets", {"kind": "preset_videos", "ids": ["problem-solution"]})
    check(res["isError"] and "keyframe-first" in res["content"][0]["text"], "a recipe that needs the stitcher can't be made yet")
    six = ["vo-product-shots", "hands-only-demo", "pov-unboxing", "talk-to-camera", "car-talk", "season-gift-clip"]
    qv = call(akey, "quote_library_assets", {"kind": "preset_videos", "ids": six, "options": {"presenter": "maya"}})["structuredContent"]
    want = sum(presets.get(i).price_credits() for i in six)
    check(qv["count"] == 6 and qv["total_credits"] == want == 6381 and qv["vendor_usd"] == 31.89,
          "six video examples: 6,381 credits, $31.89 at the vendor (69 seconds at $0.4622)")
    gv = call(akey, "generate_library_assets", {"kind": "preset_videos", "ids": ["talk-to-camera", "vo-product-shots"],
                                                "options": {"presenter": "maya"},
                                                "confirm_total_credits": presets.get("talk-to-camera").price_credits()
                                                + presets.get("vo-product-shots").price_credits()})["structuredContent"]
    vj = {j["key"]: j["job_id"] for j in gv["jobs"]}
    pt = params_of(vj["talk-to-camera"])
    n_sub = len(V.submits)
    tick_until_idle()
    talk = next(s for s in V.submits[n_sub:] if "selfie-style" in s["inputs"]["prompt"])
    vo = next(s for s in V.submits[n_sub:] if "narrator" in s["inputs"]["prompt"])
    check(pt["library"] == {"kind": "preset_video", "preset": "talk-to-camera", "demo": "mug"} and pt["presenter"] == "maya"
          and pt["situation"] == "kitchen" and pt["ref_job"] == jobs["maya:kitchen"] and pt["preset"] == "talk-to-camera"
          and talk["model"] == "bytedance/seedance-2.5/reference-to-video" and len(talk["inputs"]["image_urls"]) == 2
          and "first reference image" in talk["inputs"]["prompt"] and "kitchen counter beside them" in talk["inputs"]["prompt"]
          and "Three unhurried shots" in vo["inputs"]["prompt"] and "No people and no hands" in vo["inputs"]["prompt"]
          and len(vo["inputs"]["image_urls"]) == 1 and all(job_row(j)["status"] == "done" for j in vj.values()),
          "a presenter clip starts from the presenter's finished still (first reference) and the demo packshot; a "
          "voiceover recipe sends its own shots")
    qs = call(akey, "quote_library_assets", {"kind": "presenter_voices", "ids": ["maya", "nia"]})["structuredContent"]
    check(qs["total_credits"] == 2 * 247 and qs["vendor_usd"] == 2.47, "voice samples: six seconds at the 480p rate each")
    res = call(akey, "quote_library_assets", {"kind": "presenter_voices", "ids": ["cal"]})
    check(res["isError"] and "first still" in res["content"][0]["text"], "a voice sample needs the presenter's still")
    gs = call(akey, "generate_library_assets", {"kind": "presenter_voices", "ids": ["maya"],
                                                "confirm_total_credits": 247})["structuredContent"]
    n_sub = len(V.submits)
    tick_until_idle()
    vs = V.submits[n_sub]["inputs"]
    check(vs["resolution"] == "480p" and vs["duration"] == 6 and len(vs["image_urls"]) == 1 and "Hi, I'm Maya." in vs["prompt"]
          and "Trail Mug" not in vs["prompt"] and job_row(gs["jobs"][0]["job_id"])["status"] == "done",
          "the voice sample is the presenter alone, from their still, saying one neutral line")

    # ------------------------------------------------------------------ 8. the admin page and the export
    print("== the admin page and the export")
    page = adm.get("/admin/library")
    html = page.get_data(as_text=True)
    check(page.status_code == 200 and "Quote, then generate" in html and "presenter__maya__kitchen.png" in html
          and "talking-head-cutaways" in html and "needs keyframe_video" in html and "Dapplewood" not in html.split("Made here")[0]
          and "/media/" in html, "the page lists what was made with thumbnails, the roster and the presets")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "quote", "kind": "presenter_stills", "ids": "walt:kitchen"})
    html = r.get_data(as_text=True)
    check(r.status_code == 200 and "1 job: 40 credits" in html and 'name="total" value="40"' in html, "the page quotes first")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "generate", "kind": "presenter_stills", "ids": "walt:kitchen",
                                         "total": "39"})
    check("Quote it again" in r.get_data(as_text=True), "and its Generate needs the quoted total too")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "generate", "kind": "presenter_stills", "ids": "walt:kitchen",
                                         "total": "40"})
    check(r.status_code == 302, "with it, the job starts")
    tick_until_idle()
    z = zipfile.ZipFile(io.BytesIO(adm.get("/admin/library/export.zip").data))
    files = set(z.namelist())
    manifest = json.loads(z.read("manifest.json"))
    check({"presenter__maya__kitchen.png", "presenter__nia__sofa.png", "presenter__walt__kitchen.png", "preset__offer.png",
           "preset__talk-to-camera.mp4", "voice__maya.mp4", "demo__mug.png", "manifest.json"} <= files
          and "presenter__otis__gym.png" not in files and len(manifest) == len(files) - 1
          and all(m["file"] in files for m in manifest),
          "the export holds one finished file per tag, named by its tags, with a manifest")
    with db.connect() as conn:
        conn.execute("INSERT INTO job_reviews (job_id, user_id, workspace_id, verdict, note, at) VALUES (?,?,?,?,?,?)",
                     (jobs["nia:sofa"], au["id"], AWS, "rejected", "", db.now()))
        conn.commit()
    check("presenter__nia__sofa.png" not in zipfile.ZipFile(io.BytesIO(adm.get("/admin/library/export.zip").data)).namelist(),
          "a rejected asset is left out of the export")
    hf.vendor_presets = lambda *a, **k: {"items": [{"id": "abc", "type": "ads", "name": "Their preset"}], "cursor": None, "total": 1}
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "vendor_presets"})
    check("Their preset" in r.get_data(as_text=True) and all(p.vendor_preset == "" for p in presets.LIBRARY),
          "the vendor's own preset catalog can be read from the page; no preset references one yet")

    # ------------------------------------------------------------------ 9. the import script
    print("== tools/import_library_assets.py")
    src = os.path.join(TMP, "export")
    os.makedirs(src)
    z.extractall(src)
    noisy = Image.effect_noise((1024, 1536), 80).convert("RGB")       # a picture that doesn't compress
    noisy.save(os.path.join(src, "presenter__nia__desk.png"))
    Image.new("RGB", (1200, 900), (10, 120, 90)).save(os.path.join(src, "preset__closeup.jpg"))
    open(os.path.join(src, "presenter__nobody__kitchen.png"), "wb").write(png_bytes())
    open(os.path.join(src, "presenter__maya__gym.png"), "wb").write(png_bytes())
    open(os.path.join(src, "preset__nope.png"), "wb").write(png_bytes())
    open(os.path.join(src, "notes.txt"), "w").write("x")
    for f in os.listdir(src):                         # clips and voice samples need ffmpeg and a real file: not here
        if f.endswith(".mp4"):
            os.remove(os.path.join(src, f))
    dry = import_library_assets.run(src, STATIC, dry=True)
    check(dry["written"] and not os.listdir(os.path.join(STATIC, "presets")), "--dry-run writes nothing")
    res = import_library_assets.run(src, STATIC)
    wrote = {os.path.relpath(p, STATIC) for p in res["written"]}
    skipped = dict(res["skipped"])
    check({"presenters/maya/kitchen.webp", "presenters/maya/portrait.webp", "presenters/nia/desk.webp",
           "presenters/nia/portrait.webp", "presets/offer.webp", "presets/closeup.webp"} <= wrote and not res["failed"]
          and "no presenter" in skipped["presenter__nobody__kitchen.png"] and "no situation" in skipped["presenter__maya__gym.png"]
          and "no preset" in skipped["preset__nope.png"] and "stays in the workspace" in skipped["demo__mug.png"]
          and "notes.txt" in skipped, "tagged files land in the static layout; unknown names are skipped and listed")
    sizes = {}
    for rel in wrote:
        with Image.open(os.path.join(STATIC, rel)) as im:
            sizes[rel] = (im.format, im.size, os.path.getsize(os.path.join(STATIC, rel)))
    ratio = lambda wh: round(wh[0] / wh[1], 3)  # noqa: E731
    check(all(f == "WEBP" and b <= 200 * 1024 for f, _s, b in sizes.values())
          and abs(ratio(sizes["presenters/nia/desk.webp"][1]) - 9 / 16) < 0.005 and sizes["presenters/nia/desk.webp"][2] > 100000
          and ratio(sizes["presenters/maya/portrait.webp"][1]) == 0.8 and abs(ratio(sizes["presenters/nia/portrait.webp"][1]) - 0.8) < 0.005
          and ratio(sizes["presets/closeup.webp"][1]) == 0.8 and sizes["presenters/maya/kitchen.webp"][1] == (720, 1280),
          "every picture is WebP of at most 200 KB: stills 9:16, portraits and wide examples 4:5")
    nia = presenters.get("nia")
    check(nia["portrait"] == "/static/presenters/nia/portrait.webp" and nia["situations"][0]["still"].endswith("/nia/desk.webp")
          and presets.example_url("offer") == "/static/presets/offer.webp" and presets.example_url("closeup")
          and presets.example_video_url("offer") is None and presets.get("offer").contract()["example"] == "/static/presets/offer.webp",
          "after the import the roster and the presets give the urls")
    check(import_library_assets.main([src, "--static", STATIC, "--dry-run"]) == 0 and import_library_assets.main([]) == 2,
          "the script's command line: a folder, --static, --dry-run")
    q = call(akey, "quote_library_assets", {"kind": "preset_examples", "ids": ["offer", "shelf", "hero"],
                                           "options": {"only_missing": True}})["structuredContent"]
    check([ln["key"] for ln in q["lines"]] == ["hero"], "only_missing skips presets that already have their example")
    q = call(akey, "quote_library_assets", {"kind": "presenter_stills", "ids": ["nia:kitchen"]})["structuredContent"]
    check(q["lines"][0]["starts_from"] == os.path.join("presenters", "nia", "desk.webp"),
          "once a first still is approved and imported, new situations start from that file")

    # ------------------------------------------------------------------ 10. the claims check is free
    print("== the claims check costs a member nothing")
    spoken = {"product": PID, "type": "voiceover", "items": ["Still drinking cold coffee by ten?"],
              "body": "The Trail Mug keeps drinks hot for 6 hours. Grab yours today.", "voices": ["narrator_deep"],
              "duration": 10, "aspects": ["9:16"], "photos": [PH]}
    check(claims_check.on() is False and claims_check.estimate() == 0 and claims_check.daily_max() == 3000
          and claims_check.ws_daily_max() == 150, "CLAIMS_CHECK stays off by default; its price to a member is 0; the caps have defaults")
    os.environ["CLAIMS_CHECK"] = "1"
    balm, n_model = db.balance(ws_row(MWS)), len(model_calls)
    answers.append({"sentences": [{"line": 1, "sentence": "The Trail Mug keeps drinks hot for 6 hours.", "supported": True,
                                   "source": "claim", "quote": "Keeps drinks hot for 6 hours"}]})
    r = post(mem, "/app/create/claims", spoken).get_json()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM claims_checks WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (MWS,)).fetchone()
        holds = conn.execute("SELECT COUNT(*) FROM credit_holds WHERE workspace_id=?", (MWS,)).fetchone()[0]
    check(r["status"] == "ok" and r["charged"] == 0 and r["balance"] == balm == db.balance(ws_row(MWS)) and len(model_calls) == n_model + 1
          and row["usd"] > 0 and holds == 0, "a check runs the model, takes no credits and holds none; what it cost us is kept")
    again = post(mem, "/app/create/claims", spoken).get_json()
    check(again.get("cached") and again["charged"] == 0 and len(model_calls) == n_model + 1, "the same words again come from the cache")
    os.environ["CLAIMS_CHECK_WS_DAILY_MAX"] = "1"
    other = dict(spoken, body="The Trail Mug keeps drinks hot for 6 hours. Order today.")
    r = post(mem, "/app/create/claims", other).get_json()
    check(r["status"] == "skipped" and r["charged"] == 0 and len(model_calls) == n_model + 1,
          "past the daily cap the check is skipped (no model call), never charged")
    os.environ["CLAIMS_CHECK_WS_DAILY_MAX"] = "50"
    os.environ["CLAIMS_CHECK_DAILY_MAX"] = "1"
    r = post(mem, "/app/create/claims", dict(spoken, body="The Trail Mug keeps drinks hot for 6 hours. See it now.")).get_json()
    check(r["status"] == "skipped" and len(model_calls) == n_model + 1, "the site-wide cap does the same")
    for k in ("CLAIMS_CHECK", "CLAIMS_CHECK_WS_DAILY_MAX", "CLAIMS_CHECK_DAILY_MAX"):
        os.environ.pop(k)
    boot = json.loads(mem.get(f"/app/create?product={PID}").get_data(as_text=True).split(
        '<script id="cf-boot" type="application/json">')[1].split("</script>")[0])
    check(boot["quote_claims"] == 0 and "season-autumn" in boot["layouts"] and "then-now" in boot["layouts"]
          and len(boot["presenters"]) == 8, "Create's page: the check is priced at 0, the new statics are listed, the 8 presenters stay")

    # ------------------------------------------------------------------ 11. MCP: presets, roster, preset + presenter
    print("== MCP: list_ad_options and the ad tools")
    opt = call(mkey, "list_ad_options")["structuredContent"]
    by = {p["id"]: p for p in opt["presets"]}
    check(len(opt["presets"]) >= 30 and by["offer"]["tags"] == {"category": ["any"], "use": ["offer"]}
          and by["offer"]["example_image"] == BASE + "/static/presets/offer.webp"
          and by["hero"]["example_image"] is None and by["offer"]["layers"][0]["id"] == "badge"
          and by["car-talk"]["situation"] == "car" and by["car-talk"]["seconds"] == 8 and by["vo-product-shots"]["beats"]
          and "problem-solution" not in by and "copy-trend" not in by and by["then-now"]["tags"]["use"][0] == "before-after-honest"
          and all(p["what_for"] and p["tags"]["use"] and p["from_credits"] > 0 for p in opt["presets"]),
          f"list_ad_options: {len(opt['presets'])} presets with what_for, tags, price and example urls; none that can't run yet")
    rn = {p["id"]: p for p in opt["roster"]}
    check(len(opt["roster"]) == len(ro) and rn["nia"]["portrait"] == BASE + "/static/presenters/nia/portrait.webp"
          and rn["nia"]["situations"][0]["still"].startswith(BASE + "/static/presenters/nia/")
          and rn["walt"]["voice"] == {"id": "steady_m60", "label": "Steady, older"} and rn["cal"]["portrait"] is None
          and len(opt["presenters"]) == len(ro) and len(opt["situations"]) == 8 and len(opt["voices"]) == 6,
          "and the presenter roster, with absolute urls for the files that exist")
    aopt = call(akey, "list_ad_options")["structuredContent"]
    check(next(p for p in aopt["presets"] if p["id"] == "problem-solution")["not_available_yet"] == "keyframe_video",
          "(a site admin also sees the recipes that are waiting, marked as such)")
    a1 = {"ad_type": "ugc", "product_id": PID, "lines": ["Meet the mug. Try it today."], "presenter": "walt",
          "situation": "car", "preset": "talk-to-camera"}
    q = call(mkey, "quote_ad_batch", a1)["structuredContent"]
    check(q["ads"] == 1 and q["total_credits"] == 740 and q["lines"][0]["label"] == "Walt · Car · 9:16",
          "quote_ad_batch takes a preset, one presenter and a situation (the preset's own length)")
    g2 = call(mkey, "generate_video_ads", dict(a1, confirm_total_credits=740))["structuredContent"]
    jm = db.connect().execute("SELECT * FROM jobs WHERE batch_id=?", (g2["batch_id"],)).fetchone()
    pm = json.loads(jm["params_json"])
    check(g2["queued"] == 1 and pm["presenter"] == "walt" and pm["situation"] == "car" and pm["preset"] == "talk-to-camera"
          and pm["duration"] == 8, "generate_video_ads makes it with those")
    a2 = {"ad_type": "image", "product_id": PID, "lines": ["Hot at noon"], "preset": "offer", "layers": {"badge": "$24"}}
    q = call(mkey, "quote_ad_batch", a2)["structuredContent"]
    g3 = call(mkey, "generate_image_ads", dict(a2, confirm_total_credits=q["total_credits"]))["structuredContent"]
    ji = db.connect().execute("SELECT * FROM jobs WHERE batch_id=?", (g3["batch_id"],)).fetchone()
    pim = json.loads(ji["params_json"])
    check(q["total_credits"] == 22 and pim["preset"] == "offer" and pim["layout"] == "offer" and pim["layers"]["badge"] == "$24"
          and pim["aspect"] == "4:5" and "Trail Mug centred" in ji["brief"] and ji["template_id"] == "offer",
          "generate_image_ads takes an image preset: its scene and shape come from the preset, the words from the caller")
    for bad, why in ((dict(a2, layers={"badge": "$19"}), "price or the offer saved"),
                     (dict(a1, preset="problem-solution"), "isn't available yet"),
                     (dict(a1, preset="offer"), "image preset"), (dict(a1, preset="nope"), "Unknown preset")):
        res = call(mkey, "quote_ad_batch", bad)
        check(res["isError"] and why in res["content"][0]["text"], f"refused: {why}")
    q = call(mkey, "quote_ad_batch", {"ad_type": "ugc", "product_id": PID, "lines": ["Meet the mug."], "presenters": ["maya", "theo"],
                                      "duration": 10})["structuredContent"]
    check(q["ads"] == 2 and q["total_credits"] == 2 * 925 and q["lines"][0]["label"] == "Maya · 9:16",
          "the existing arguments (presenters, duration) work as before")

    # ------------------------------------------------------------------ nothing left this machine
    print("== no vendor was really called")
    check(leaks == [], "no connection left this machine during the whole suite")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} m15 library checks passed")

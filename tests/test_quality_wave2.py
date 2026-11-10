"""Quality wave 2 (the audit's bigger builds 1, 3 for images, 4 and 7, plus the free-fix plumbing), on a throwaway
database:

  * the preset library: one record per preset with the whole contract; the 5 ad types and 14 templates moved onto
    it with the same outputs and prices (tests/fixtures/presets_snapshot.json, taken before the move); every
    product category gets at least 4 distinct statics; Create lists presets with a "what it's for" line and a
    thumbnail that exists
  * the text and logo layer: headline, subhead, badge, button, callouts, review card, comparison and logo drawn
    by Pillow in the brand kit's colours and font, inside the safe area of every shape, auto-fitted, with a
    contrast of at least 4.5:1; an offer badge can only be the product's saved price or offer; a review needs
    the member's word that it is real and is never written by AI
  * the claims check (CLAIMS_CHECK, default off): one model call; a "supported" verdict only stands when its quote
    really is in the sources; flagged lines block Generate; the result is cached so Generate doesn't pay twice
  * automatic image QA (IMAGE_QA, default off): pass, one silent retry of only the failed pictures, a full or
    partial refund on the second failure, a row per picture for the acceptance numbers; the check failing to run
    never holds an ad back
  * one free fix per delivered picture (FREE_FIX, default off): 0 credits, once per picture, a monthly cap per
    workspace, released when the fix fails on our side

No network: every vendor is faked, and a socket that tries to leave the machine fails the test.

    python tests/test_quality_wave2.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import shutil
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_qw2_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("FIRST_RUN_NUDGES", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "RS_FREE_PREVIEW", "RS_TALKING", "RS_SERVICE_PATH",
          "CLAIMS_CHECK", "IMAGE_QA", "FREE_FIX", "FREE_FIX_MONTHLY_CAP", "TEXT_LAYERS", "IMAGE_QA_MODEL",
          "IMAGE_QA_DAILY_MAX", "RS_MARKUP", "RS_HD_MARKUP", "RS_TALKING_DURATIONS", "ANTHROPIC_FAST_MODEL",
          "LLM_USD_IN_PER_MTOK", "LLM_USD_OUT_PER_MTOK", "GROQ_API_KEY"):
    os.environ.pop(k, None)
# The keys are made-up strings: nothing here may reach a vendor with them (see the socket guard below).
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_PUBLIC_URL="https://campaignify.test", ANTHROPIC_API_KEY="sk-ant-test", RS_DISK_MIN_GB="0.2",
                  ADS_READY_EMAIL="0")

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
import db  # noqa: E402
import free_fix  # noqa: E402
import hf  # noqa: E402
import image_qa  # noqa: E402
import llm  # noqa: E402
import presets  # noqa: E402
import products  # noqa: E402
import textlayer  # noqa: E402
import worker  # noqa: E402
from PIL import Image  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import presets_snapshot  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.22.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.environ_base["HTTP_USER_AGENT"] = UA
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def pixels(im):
    raw = im.convert("RGB").tobytes()
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]


def png_bytes(size=(640, 800), color=(210, 200, 185)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def signup(email, credits=0):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        if credits:
            conn.execute("UPDATE workspaces SET credits=? WHERE id=?", (credits, u["workspace_id"]))
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


def outs_of(jid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM outputs WHERE job_id=? ORDER BY id", (jid,)).fetchall()


# ---------------------------------------------------------------- the fake vendors
class Vendor:
    """The generation vendor, in memory: submit returns a request id, status says completed with one picture URL
    per batch_size, and the "download" of those URLs is served from here too."""

    def __init__(self):
        self.submits, self.uploads, self.keys, self.n = [], 0, [], 0
        self.files = {}

    def upload(self, data, ctype):
        self.uploads += 1
        return f"https://files.example/up{self.uploads}.png"

    def submit(self, model, inputs, webhook_url=None, idempotency_key=None):
        self.n += 1
        self.submits.append({"model": model, "inputs": inputs, "key": idempotency_key})
        self.keys.append(idempotency_key)
        return f"req-{self.n}"

    def status(self, rid):
        i = int(rid.split("-")[1])
        n = int(self.submits[i - 1]["inputs"].get("batch_size") or 1)
        urls = [f"https://cdn.example/{rid}-{k}.png" for k in range(1, n + 1)]
        for u in urls:
            self.files[u] = png_bytes()
        return {"status": "completed", "images": [{"url": u} for u in urls]}


V = Vendor()
hf.upload, hf.submit, hf.status = V.upload, V.submit, V.status
hf.estimate = lambda *a, **k: None
hf.cancel = lambda rid: hf.CANCELED
hf.outputs = lambda res: [{"kind": "image", "url": i["url"]} for i in res.get("images") or []]


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

# The model, faked at the lowest level (llm._anthropic): every call is recorded and answered from a queue of
# tool results, so llm.structured, its image handling and the usage maths all run for real.
model_calls, answers = [], []


def _fake_anthropic(body, timeout=None):
    model_calls.append(body)
    if not answers:
        raise AssertionError("an unexpected model call: " + body["tools"][0]["name"])
    ans = answers.pop(0)
    if isinstance(ans, Exception):
        raise ans
    return {"model": body["model"], "usage": {"input_tokens": 1800, "output_tokens": 300},
            "content": [{"type": "tool_use", "name": body["tools"][0]["name"], "input": ans}]}


llm._anthropic = _fake_anthropic
GOOD = {"product_present": True, "photo_has_label": True, "label_matches": True, "label_legible": True, "other_text": [],
        "garbled_text": False, "people": False, "deformed_hands": False, "deformed_faces": False}
BAD_LABEL = dict(GOOD, label_matches=False)

try:
    # ------------------------------------------------------------------ 1. the preset library
    print("== presets: one record per preset, the old ones unchanged")
    snap = json.load(open(os.path.join(HERE, "fixtures", "presets_snapshot.json"), encoding="utf-8"))
    now = json.loads(json.dumps(presets_snapshot.snapshot()))
    for key in sorted(snap):
        check(now[key] == snap[key], f"snapshot: {key} is what it was before the move")
    check(len(presets.TYPES) == 5 and len(presets.OLD_TEMPLATES) == 14 and len(catalog.TEMPLATES) == 14
          and catalog.AD_TYPES == [p.legacy for p in presets.TYPES], "the 5 ad types and 14 templates are preset records")
    need = ("id", "kind", "name", "what_for", "format", "aspects", "inputs", "recipe", "model", "params", "beats",
            "text_layers", "qa", "locks", "price")
    for p in presets.LIBRARY:
        c = p.contract()
        missing = [k for k in need if k not in c]
        ok = (not missing and c["what_for"] and c["inputs"] and c["recipe"] and c["model"] and c["beats"] and c["qa"]
              and c["price"]["credits"] > 0 and c["format"] in ("image", "video", "talking"))
        if not ok:
            check(False, f"preset {p.id}: the contract is complete ({missing or c})")
    check(True, f"all {len(presets.LIBRARY)} presets carry the whole contract (format, shapes, inputs, recipe, model and "
                "settings, beats, text layers, QA checks, price)")
    check(all(presets.get(t["id"]).price_credits() == catalog.price_credits("ad_image", batch=1)
              for t in catalog.TEMPLATES if t["tool"] == "ad_image")
          and presets.get("turn").price_credits() == catalog.price_credits("photo_to_video", duration=5)
          and presets.get("how").price_credits() == catalog.price_credits("photo_to_video", duration=10)
          and presets.get("ugc").price_credits(duration=10) == catalog.price_credits("ugc_video", duration=10)
          and presets.get("offer").price_credits(batch=2) == catalog.price_credits("ad_image", batch=2),
          "a preset's price is the same table price as before; the new statics cost what an image ad costs")
    check(presets.get("ugc").locks == ("product", "presenter_still", "voice") and presets.IDENTITY["wired"] is False
          and "one approved still per presenter situation" in presets.IDENTITY["presenter"],
          "the presenter presets carry the identity lock from the paid test (one approved still), not wired yet")
    for cat_code, _name in products.CATEGORIES + [("", "no category")]:
        prod = {"name": "Thing", "category": cat_code, "description": "", "tags": ""}
        sts = presets.statics_for(prod, layered=False)
        ids = [p.id for p in sts]
        own = [p for p in sts if isinstance(p.fits, tuple)]
        check(len(set(ids)) == len(ids) >= 4 and not any(p.fits == "coffee" for p in sts)
              and len({p.recipe for p in sts}) == len(sts)
              and (len(own) >= 2 or cat_code in ("", "other")),
              f"category {cat_code or '(none)'}: {len(ids)} distinct non-coffee statics"
              + (f", {len(own)} written for it" if own else ""))
    check([p.id for p in presets.statics_for({"name": "Instant coffee"}, layered=False)][:4] == ["two-pm", "iced", "cost", "gym"],
          "coffee keeps its own templates, first")
    brandk = {"name": "Northwind", "product": "Trail Mug"}
    for p in presets.SCENES + presets.LAYERED:
        brief = catalog.fill(p.recipe, brandk)
        parts = catalog.split_image_brief(brief)
        scene = catalog.scene_only(parts["scene"])
        ok = (compliance.check(brief)["ok"] and "Trail Mug" in scene and "{" not in brief
              and len(scene) > 0.8 * len(parts["scene"])
              and os.path.exists(os.path.join(ROOT, "static", "previews", p.preview + ".jpg"))
              and p.id in catalog.TEMPLATE_BY_ID and (p.layout or parts["headline"]))
        if not ok:
            check(False, f"new preset {p.id}: its brief passes the rules, survives the scene filter and has a thumbnail")
    check(True, f"the {len(presets.SCENES) + len(presets.LAYERED)} new presets: rule-clean briefs, a headline (or a text "
                "layer), a thumbnail file, and a place in TEMPLATE_BY_ID")
    pf = catalog.template_prefill(catalog.TEMPLATE_BY_ID["offer"], brandk)
    check(pf["layout"] == "offer" and pf["items"] == [] and "Trail Mug" in pf["body"] and pf["type"] == "image"
          and catalog.template_prefill(catalog.TEMPLATE_BY_ID["marketplace"], brandk)["items"] == ["Marketplace image"]
          and catalog.template_prefill(catalog.TEMPLATE_BY_ID["shelf"], brandk)["items"] == ["Worth the shelf space."],
          "a layered preset starts with the scene and no made-up headline; a scene preset starts with its headline")

    # ------------------------------------------------------------------ 2. the text layer
    print("== the text and logo layer")
    check(set(textlayer.FONTS) == {"sans", "serif"} and all(
        os.path.exists(os.path.join(textlayer.FONT_DIR, f)) for fam in textlayer.FONTS.values()
        for k, f in fam.items() if k != "name")
        and all("SIL Open Font License" in open(os.path.join(textlayer.FONT_DIR, n), encoding="utf-8").read()
                for n in ("OFL-Poppins.txt", "OFL-DMSerifDisplay.txt"))
        and "Poppins" in open(os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), encoding="utf-8").read(),
        "the fonts are bundled with their OFL licence files and listed in THIRD_PARTY_NOTICES.md")
    check(round(textlayer.contrast((255, 255, 255), (0, 0, 0)), 1) == 21.0 and textlayer.contrast((119, 119, 119), (255, 255, 255)) < 4.5
          and all(textlayer.contrast(textlayer.on_color((v, v, v)), (v, v, v)) >= 4.5 for v in range(0, 256, 5)),
          "contrast is the WCAG ratio; text on a solid button or badge is always at least 4.5:1")
    logo = os.path.join(TMP, "logo.png")
    Image.new("RGBA", (300, 120), (20, 90, 70, 255)).save(logo)
    kit = {"primary": "#0e5e4e", "accent": "#ffd23f", "font": "serif", "logo": logo}
    words = {"headline": "The mug that keeps up with you", "subline": "Hot for six hours on the trail",
             "cta": "Shop now", "badge": "$24", "callouts": ["Leakproof lid", "Fits a cup holder", "Dishwasher safe"],
             "review": {"quote": "I didn't expect much, but it has been in my bag every single morning since it arrived.",
                        "name": "Dana R.", "stars": 4},
             "compare": {"left_title": "The usual way", "right_title": "Trail Mug", "left": ["Cold by ten", "Leaks", "Bulky"],
                         "right": ["Hot at noon", "Sealed lid", "Fits a pocket"]}}
    sizes = {"9:16": (720, 1280), "4:5": (800, 1000), "1:1": (900, 900), "16:9": (1280, 720)}
    drawn = 0
    for asp, size in sizes.items():
        for tone in ((245, 240, 230), (24, 26, 30), (128, 128, 128)):
            base = Image.new("RGB", size, tone)
            for layout in ("headline", "offer", "callouts", "review", "compare"):
                out, rep = textlayer.render(base, layout, words, kit, asp)
                bad = [(n, v) for n, v in rep["layers"].items()
                       if not v["inside"] or v.get("fit") is False or v.get("contrast", 99) < textlayer.MIN_CONTRAST]
                if bad or not rep["ok"] or out.size != size or out.tobytes() == base.tobytes():
                    check(False, f"{layout} {asp} on {tone}: every layer inside the safe area, fitted, contrast >= 4.5 ({bad})")
                drawn += len(rep["layers"])
    check(drawn > 150, f"all five layouts in all four shapes on light, dark and mid backgrounds: {drawn} layers, each inside "
                       "its safe area, fitted, at 4.5:1 or better")
    sx0, sy0, sx1, sy1 = textlayer.safe_box((720, 1280), "9:16")
    check(sy0 >= 0.14 * 1280 - 1 and sy1 <= 0.80 * 1280 + 1 and textlayer.safe_box((800, 1000), "4:5")[1] == 60,
          "a 9:16 story keeps its top 14% and bottom 20% clear; a 4:5 feed image only a small margin")
    busy = Image.new("RGB", (800, 1000), (250, 250, 250))
    busy.paste((8, 8, 8), (0, 0, 400, 1000))
    _, rep = textlayer.render(busy, "headline", {"headline": "Half dark, half light, all the way"}, kit)
    check(rep["layers"]["headline"]["scrim"] is True and rep["layers"]["headline"]["contrast"] >= 4.5 and rep["ok"],
          "words over a picture that is both dark and light get a panel behind them, and still reach 4.5:1")
    _, rep = textlayer.render(Image.new("RGB", (800, 1000), (250, 250, 250)), "headline", {"headline": "Light"}, kit)
    check(rep["layers"]["headline"]["scrim"] is False and rep["layers"]["headline"]["color"] == [17, 17, 17],
          "over a light picture the words are dark, with no panel")
    short = textlayer.fit_text("Hot", "sans", "bold", 600, 200, 80, 30)
    longer = textlayer.fit_text("The mug that keeps every drink hot for a very long time on the trail", "sans", "bold", 600, 200, 80, 30)
    cut = textlayer.fit_text("word " * 80, "sans", "bold", 300, 60, 40, 30, max_lines=2)
    check(short["px"] == 80 and 30 <= longer["px"] < 80 and longer["fit"] and len(longer["lines"]) <= 3
          and cut["fit"] is False and cut["lines"][-1].endswith("…") and len(cut["lines"]) <= 2,
          "auto-fit: short text stays large, long text steps down, text that can't fit is cut and reported")
    _, rep = textlayer.render(Image.new("RGB", (800, 1000), (240, 240, 240)), "offer", words, kit)
    check("logo" in rep["layers"] and rep["layers"]["logo"]["inside"], "the brand's logo file is placed inside the safe area")
    _, rep = textlayer.render(Image.new("RGB", (900, 900), (255, 255, 255)), "none", words, kit)
    check(rep["layers"] == {}, "the marketplace image gets no layer at all")
    f = os.path.join(TMP, "pic.png")
    Image.new("RGB", (800, 1000), (230, 220, 200)).save(f)
    textlayer.apply_to_file(f, "offer", words, kit, "4:5")
    check(os.path.exists(textlayer.base_path(f)) and Image.open(textlayer.base_path(f)).tobytes() != Image.open(f).tobytes()
          and Image.open(textlayer.base_path(f)).getpixel((5, 5)) == (230, 220, 200),
          "the text-free picture is kept beside the finished one")
    check(textlayer.kit_of(None) == textlayer.DEFAULT_KIT and textlayer.kit_of(
        {"kit_primary": "#ABCDEF", "kit_accent": "nope", "kit_font": "comic", "kit_logo": "/nowhere.png", "brand_name": "N"})
        == dict(textlayer.DEFAULT_KIT, primary="#abcdef", brand="N"),
        "the brand kit falls back to defaults part by part")

    b_ok = {"product": "Trail Mug", "price": "$24", "offer": "Free lid with every mug"}
    offer = presets.get("offer")
    check(presets.clean_layers(offer, {"badge": "$24"}, b_ok) == {"badge": "$24"}
          and presets.clean_layers(offer, {"badge": "free lid"}, b_ok)["badge"] == "free lid",
          "an offer badge may show the product's saved price, or its saved offer")
    for raw, brief, why in (({"badge": "$19"}, b_ok, "can only show the price or the offer saved"),
                            ({"badge": "$24"}, {"product": "Trail Mug"}, "save the price or the offer on the product first"),
                            ({"badge": ""}, b_ok, "this preset needs one")):
        try:
            presets.clean_layers(offer, raw, brief)
            got = ""
        except presets.LayerError as e:
            got = str(e)
        check(why in got, f"badge {raw['badge']!r}: refused ({why})")
    review = presets.get("review")
    try:
        presets.clean_layers(review, {"reviewer": "Dana", "stars": 5}, b_ok)
        got = ""
    except presets.LayerError as e:
        got = str(e)
    check("confirm the reviews are real" in got and presets.clean_layers(
        review, {"reviewer": "Dana", "stars": "5", "attest": True}, b_ok) == {"reviewer": "Dana", "stars": 5, "attest": True}
        and presets.layout_spec(review)["ai_items"] is False,
        "a review card needs the member's word that the review is real, and the AI writer is never offered for it")
    try:
        presets.clean_layers(presets.get("callouts"), {"callouts": ["Cures anxiety"]}, b_ok)
        got = ""
    except presets.LayerError as e:
        got = str(e)
    check(got.startswith("Features:"), "callouts go through the compliance rules")

    # ------------------------------------------------------------------ a member, a product, a photo
    c, u = signup("maker@example.com", credits=2000)
    WS = u["workspace_id"]
    photo_path = os.path.join(TMP, "product.png")
    open(photo_path, "wb").write(png_bytes((600, 600), (90, 110, 130)))
    PAGE = ("Trail Mug by Northwind. Double-wall steel keeps drinks hot for 6 hours. Holds 12 oz. The lid is leakproof "
            "and the mug fits a standard cup holder. Dishwasher safe. Ships in 2 days.")
    with appmod.app.test_request_context("/"):
        from flask import g
        with db.connect() as conn:
            g.user = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
            g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (WS,)).fetchone()
            PID = products.save_kit(conn, g.ws, brand_name="Northwind", product_name="Trail Mug", audience="Hikers",
                                    claims="Keeps drinks hot for 6 hours")
            PH = products.add_photo(conn, WS, PID, photo_path, "product.png", "image/png")
            conn.execute("UPDATE products SET price='$24', offer='Free lid with every mug', import_json=? WHERE id=?",
                         (json.dumps({"url": "https://northwind.example/mug", "images": [], "text": PAGE}), PID))
            BID = conn.execute("SELECT brand_id FROM products WHERE id=?", (PID,)).fetchone()[0]
            conn.execute("UPDATE brands SET kit_primary='#0e5e4e', kit_accent='#ffd23f', kit_font='serif', kit_logo=? WHERE id=?",
                         (logo, BID))
            conn.commit()

    # ------------------------------------------------------------------ 3. Create: the list, a layered static end to end
    print("== Create lists presets; a static with a text layer, end to end")
    page = c.get(f"/app/create?product={PID}").get_data(as_text=True)
    boot = json.loads(page.split('<script id="cf-boot" type="application/json">')[1].split("</script>")[0])
    cards = {t["id"]: t for t in boot["templates"]}
    listed = [i for i in boot["product"]["templates"] if i in cards]
    check(len(listed) >= 12 and {"hero", "shelf", "offer", "review", "marketplace", "turn"} <= set(listed)
          and "two-pm" not in listed and all(cards[i]["what_for"] and cards[i]["preview"].endswith(".jpg")
                                             and cards[i]["from"] > 0 for i in listed),
          f"Create offers {len(listed)} presets for this product, each with a one-line \"what it's for\", a thumbnail and a price")
    check(all(c.get(cards[i]["preview"]).status_code == 200 for i in listed), "every listed thumbnail is served")
    check("Or start from a preset" in page and "data-presets" in page and set(boot["layouts"]) == {
        "clean-headline", "offer", "callouts", "review", "compare", "marketplace"} and boot["claims_check"] is False,
        "the page carries the preset list and the layered presets' fields; the claims check is off by default")
    pf = c.get(f"/app/create?product={PID}&template=offer").get_data(as_text=True)
    pfb = json.loads(pf.split('<script id="cf-boot" type="application/json">')[1].split("</script>")[0])["prefill"]
    check(pfb["template"] == "offer" and pfb["layout"] == "offer" and pfb["type"] == "image", "?template=offer prefills the flow")

    cfg = {"product": PID, "type": "image", "preset": "offer", "items": ["The mug that keeps up"], "subline": "Hot for hours",
           "cta": "Shop now", "body": catalog.fill(presets.get("offer").recipe, {"product": "Trail Mug"}),
           "aspects": ["4:5"], "versions": 1, "layers": {"badge": "$24"}, "photos": [PH]}
    r = post(c, "/app/create/check", dict(cfg, layers={"badge": "$5"})).get_json()
    check(not r["ok"] and "can only show the price or the offer" in r["text_blocks"][0], "the free check names a badge that isn't the saved price")
    q = post(c, "/app/create/quote", cfg).get_json()
    check(q["count"] == 1 and q["total"] == catalog.price_credits("ad_image", batch=1) == 22, "an offer static costs what an image ad costs (22 credits)")
    bad = post(c, "/app/create/quote", dict(cfg, layers={"badge": "50% off"})).get_json()
    check(bad["count"] == 0 and bad["step"] == "script", "a badge that isn't the product's price or offer can't be quoted")
    r = post(c, "/app/create/generate", dict(cfg, total=q["total"]))
    check(r.status_code == 200 and r.get_json()["ok"], "it generates")
    with db.connect() as conn:
        J1 = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (WS,)).fetchone()
    p1 = json.loads(J1["params_json"])
    check(p1["layout"] == "offer" and p1["layers"] == {"badge": "$24"} and p1["preset"] == "offer" and J1["template_id"] == "offer"
          and J1["credits"] == 22, "the job carries its preset, layout and layer words")
    n_calls = len(model_calls)
    worker.tick()
    sub = V.submits[-1]
    pr = sub["inputs"]["prompt"]
    check(sub["model"] == "higgsfield/ads-studio/v1.0" and "No headline: add no words at all." in pr and "Shop now" not in pr
          and "The mug that keeps up" not in pr and "$24" not in pr and "Northwind" not in pr
          and presets.ZONES["offer"] in pr and catalog.NO_EXTRA_TEXT in pr and "exactly as in the reference photos" in pr,
          "the image model is asked for a picture with NO words (not the headline, the badge, the button or the brand name) "
          "and told where to leave calm space")
    worker.tick()
    J1 = job_row(J1["id"])
    o1 = outs_of(J1["id"])
    check(J1["status"] == "done" and len(o1) == 1 and o1[0]["url"] == "" and os.path.exists(o1[0]["path"])
          and os.path.exists(textlayer.base_path(o1[0]["path"])), "done: one picture, kept on disk with its text-free original")
    fin, basei = Image.open(o1[0]["path"]).convert("RGB"), Image.open(textlayer.base_path(o1[0]["path"])).convert("RGB")
    diff = sum(1 for a, b in zip(pixels(fin.resize((80, 100))), pixels(basei.resize((80, 100)))) if a != b)
    acc = (255, 210, 63)
    check(diff > 300 and any(abs(px[0] - acc[0]) < 12 and abs(px[1] - acc[1]) < 12 and abs(px[2] - acc[2]) < 12
                             for px in pixels(fin.resize((160, 200))))
          and len(model_calls) == n_calls,
          "the finished picture has the layer on it, with the badge in the brand kit's accent colour; no model was called "
          "(IMAGE_QA is off)")
    check(c.get(f"/media/{o1[0]['id']}").status_code == 200, "it is served from disk (the vendor's text-free link is never shown)")
    mk = dict(cfg, preset="marketplace", items=["Marketplace image"], layers={}, aspects=["1:1"], subline="", cta="",
              body=catalog.fill(presets.get("marketplace").recipe, {"product": "Trail Mug"}))
    _mq = post(c, "/app/create/quote", mk).get_json()
    post(c, "/app/create/generate", dict(mk, total=_mq["total"]))
    worker.tick()
    check("pure white seamless background" in V.submits[-1]["inputs"]["prompt"] and "add no words at all" in V.submits[-1]["inputs"]["prompt"],
          "the marketplace image: pure white, no words")
    worker.tick()
    rv = dict(cfg, preset="review", items=["It has been in my bag every single morning since it arrived."], subline="", cta="",
              layers={"reviewer": "Dana R.", "stars": 5})
    check(post(c, "/app/create/quote", rv).get_json()["count"] == 0
          and post(c, "/app/create/quote", dict(rv, layers={"reviewer": "Dana R.", "stars": 5, "attest": True})).get_json()["count"] == 1,
          "a review card can't be made until the member confirms the review is real")
    os.environ["TEXT_LAYERS"] = "0"
    off = json.loads(c.get(f"/app/create?product={PID}").get_data(as_text=True).split(
        '<script id="cf-boot" type="application/json">')[1].split("</script>")[0])
    check("offer" not in off["product"]["templates"] and off["layouts"] == {} and "shelf" in off["product"]["templates"]
          and post(c, "/app/create/quote", cfg).get_json()["count"] == 0,
          "TEXT_LAYERS=0 hides the layered statics and refuses new ones; scene presets stay")
    os.environ.pop("TEXT_LAYERS")
    legacy = {"product": PID, "type": "image", "preset": "shelf", "items": ["Worth the shelf space."], "aspects": ["4:5"],
              "body": "The Trail Mug on a tidy open shelf.", "photos": [PH], "versions": 1}
    lq = post(c, "/app/create/quote", legacy).get_json()
    post(c, "/app/create/generate", dict(legacy, total=lq["total"]))
    worker.tick()
    check('Headline (the main text, large and easy to read): "Worth the shelf space."' in V.submits[-1]["inputs"]["prompt"],
          "a scene preset is made exactly as image ads were: the image model sets its headline")
    worker.tick()
    with db.connect() as conn:
        JL = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (WS,)).fetchone()
    check(JL["status"] == "done" and outs_of(JL["id"])[0]["url"].startswith("https://cdn.example/")
          and not os.path.exists(textlayer.base_path(outs_of(JL["id"])[0]["path"])),
          "and saved the old way (no text layer, no check)")

    # ------------------------------------------------------------------ 4. the claims check
    print("== the claims check")
    check(claims_check.on() is False and image_qa.on() is False and free_fix.on() is False and presets.text_layers_on() is True,
          "defaults: CLAIMS_CHECK off, IMAGE_QA off, FREE_FIX off, TEXT_LAYERS on")
    brief = {"product": "Trail Mug", "claims": "Keeps drinks hot for 6 hours", "price": "$24", "description": "A steel travel mug"}
    src = claims_check.sources(brief, PAGE)
    for sent, quote, source, want in (
            ("It keeps your coffee hot for 6 hours.", "Keeps drinks hot for 6 hours", "claim", True),
            ("It keeps your coffee hot for 12 hours.", "Keeps drinks hot for 6 hours", "claim", False),
            ("The lid is leakproof.", "The lid is leakproof", "page", True),
            ("It is made from recycled ocean plastic.", "Made from recycled ocean plastic", "page", False),
            ("It costs $24.", "$24", "details", True),
            ("Doctors recommend it.", "Double-wall steel keeps drinks hot", "page", False),
            ("It holds 12 oz.", "", "page", False)):
        check(claims_check.verify(sent, quote, source, src) is want,
              f"verify: {sent!r} with quote {quote!r} -> {'supported' if want else 'not supported'}")
    lines = ["Still drinking cold coffee by ten? The Trail Mug keeps drinks hot for 6 hours. It is made from recycled ocean "
             "plastic. Grab yours today."]
    answers.append({"sentences": [
        {"line": 1, "sentence": "The Trail Mug keeps drinks hot for 6 hours.", "supported": True, "source": "claim",
         "quote": "Keeps drinks hot for 6 hours"},
        {"line": 1, "sentence": "It is made from recycled ocean plastic.", "supported": True, "source": "page",
         "quote": "made from recycled ocean plastic"},        # the model says supported with a quote that isn't there
        {"line": 9, "sentence": "A sentence that isn't in the copy.", "supported": False, "source": "none", "quote": ""}]})
    use = llm.Usage()
    res = claims_check.check(lines, brief, PAGE, use)
    body = model_calls[-1]
    check(res["status"] == "flagged" and [f["sentence"] for f in res["flags"]] == ["It is made from recycled ocean plastic."]
          and res["facts"] == 2 and res["supported"] == 1 and res["flags"][0]["line"] == 0
          and res["flags"][0]["why"] == claims_check.EDIT_OR_REMOVE,
          "one call: the backed fact passes; a 'supported' verdict whose quote isn't in the sources is flagged anyway")
    check(use.calls == 1 and body["model"] == llm.fast_model() and "Keeps drinks hot for 6 hours" in body["messages"][0]["content"]
          and "Dishwasher safe" in body["messages"][0]["content"] and body["temperature"] == 0
          and 1 <= use.credits <= claims_check.estimate() == 2,
          "it is one fast-model call through the llm wrapper, given the approved claims and the page text, priced by the same table")

    spoken = {"product": PID, "type": "voiceover", "items": ["Still drinking cold coffee by ten?"],
              "body": "The Trail Mug keeps drinks hot for 6 hours. It is made from recycled ocean plastic. Grab yours today.",
              "voices": ["narrator_deep"], "duration": 10, "aspects": ["9:16"], "photos": [PH]}
    n_calls = len(model_calls)
    check(post(c, "/app/create/claims", spoken).get_json() == {"status": "off", "flags": []} and len(model_calls) == n_calls,
          "CLAIMS_CHECK off: the endpoint does nothing and calls no model")
    sq = post(c, "/app/create/quote", spoken).get_json()
    os.environ["CLAIMS_CHECK"] = "1"
    bal0 = db.balance(ws_row(WS))
    r = post(c, "/app/create/generate", dict(spoken, total=sq["total"]))
    check(r.status_code == 400 and r.get_json().get("claims") is True and db.balance(ws_row(WS)) == bal0,
          "CLAIMS_CHECK on: Generate refuses a script that hasn't been checked, and charges nothing")
    answers.append({"sentences": [
        {"line": 1, "sentence": "The Trail Mug keeps drinks hot for 6 hours.", "supported": True, "source": "claim",
         "quote": "Keeps drinks hot for 6 hours"},
        {"line": 1, "sentence": "It is made from recycled ocean plastic.", "supported": False, "source": "none", "quote": ""}]})
    r = post(c, "/app/create/claims", spoken).get_json()
    check(r["status"] == "flagged" and len(r["flags"]) == 1 and r["flags"][0]["sentence"] == "It is made from recycled ocean plastic."
          and 1 <= r["charged"] <= 2 and r["balance"] == bal0 - r["charged"],
          f"the script step's check flags the unsupported line and costs {r['charged']} credit(s)")
    n_calls = len(model_calls)
    again = post(c, "/app/create/claims", spoken).get_json()
    check(again["status"] == "flagged" and again.get("cached") and again["charged"] == 0 and len(model_calls) == n_calls,
          "asking again for the same words is free: the result is kept")
    r = post(c, "/app/create/generate", dict(spoken, total=sq["total"]))
    check(r.status_code == 400 and r.get_json()["flags"][0]["sentence"] == "It is made from recycled ocean plastic."
          and r.get_json()["step"] == "script", "Generate refuses while a flagged line is left, and names it")
    fixed = dict(spoken, body="The Trail Mug keeps drinks hot for 6 hours. Grab yours today.")
    answers.append({"sentences": [{"line": 1, "sentence": "The Trail Mug keeps drinks hot for 6 hours.", "supported": True,
                                   "source": "claim", "quote": "Keeps drinks hot for 6 hours"}]})
    check(post(c, "/app/create/claims", fixed).get_json()["status"] == "ok", "with the line removed the check passes")
    n_calls, n_sub = len(model_calls), len(V.submits)
    r = post(c, "/app/create/generate", dict(fixed, total=sq["total"]))
    check(r.status_code == 200 and r.get_json()["ok"] and len(model_calls) == n_calls, "and Generate goes ahead without paying for the check again")
    with db.connect() as conn:      # this suite doesn't render video: take the queued talking video back out
        vj = conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND tool='ugc_video' AND status='pending'", (WS,)).fetchone()
        worker._fail(conn, vj, worker.CANCELED)
    check(len(V.submits) == n_sub and job_row(vj["id"])["status"] == "failed", "(the video is canceled before any vendor call)")
    answers.append(llm.LLMError("529: overloaded"))
    other = dict(fixed, body="The Trail Mug holds 12 oz. Grab yours today.")
    r = post(c, "/app/create/claims", other).get_json()
    check(r["status"] == "skipped" and r["flags"] == [], "when the check can't run the script is let through as 'skipped' (the fixed rules still apply)")
    imgcfg = dict(cfg, preset="callouts", layers={"callouts": ["Leakproof lid", "Bluetooth speaker"]}, cta="", subline="")
    check(claims_check.lines_of(imgcfg) == ["The mug that keeps up", "Leakproof lid", "Bluetooth speaker"]
          and claims_check.lines_of(dict(rv, layers={"attest": True})) == []
          and claims_check.lines_of({"type": "product_video", "items": ["The mug turns slowly"]}) == [],
          "image copy is checked too (headline, callouts); a customer's own review and a scene description are not")
    os.environ.pop("CLAIMS_CHECK")
    check("data-claims" in page and "Edit or remove" in open(os.path.join(ROOT, "web", "create.html"), encoding="utf-8").read(),
          "Create has the flagged-lines box with 'edit or remove'")

    # ------------------------------------------------------------------ 5. automatic image QA
    print("== automatic image QA")
    check(image_qa.judge(GOOD)["pass"] and image_qa.judge({})["pass"] is False
          and image_qa.judge(BAD_LABEL)["reasons"] == ["The label or logo doesn't match the product photo."]
          and image_qa.judge(dict(GOOD, product_present=False))["reasons"] == ["The product isn't in the picture."]
          and image_qa.judge(dict(GOOD, label_legible=False))["reasons"] == ["The label can't be read."]
          and image_qa.judge(dict(GOOD, label_legible=False, photo_has_label=False))["pass"]
          and image_qa.judge(dict(GOOD, deformed_hands=True))["reasons"] == ["A hand or a face is malformed."]
          and image_qa.judge(dict(GOOD, garbled_text=True))["reasons"] == ["Garbled text."],
          "the verdict is worked out from the model's answers, strictly; an empty answer fails")
    check(image_qa.judge(dict(GOOD, other_text=["Worth the shelf space.", "NORTHWIND"]), ["Worth the shelf space.", "Northwind"])["pass"]
          and image_qa.judge(dict(GOOD, other_text=["Worth the shelf space.", "50% OFF TODAY"]), ["Worth the shelf space."])["reasons"]
          == ["Extra text: 50% OFF TODAY"]
          and not image_qa.judge(dict(GOOD, other_text=["Shop now"]), [])["pass"],
          "extra text: the ad's own words are allowed, anything else fails; a layered static allows none")
    kf = image_qa.judge(dict(GOOD, one_clear_face=False), kind="keyframe")
    check(not kf["pass"] and kf["checks"]["keyframe"] is False and image_qa.judge(dict(GOOD, one_clear_face=True), kind="keyframe")["pass"],
          "a presenter keyframe gets the same checks plus one clear face")
    answers.append(dict(GOOD))
    res = image_qa.inspect(png_bytes(), photo_path, ["Hello"], product="Trail Mug")
    body = model_calls[-1]
    content = body["messages"][0]["content"]
    check(res["pass"] and res["usd"] > 0 and [x["type"] for x in content] == ["image", "image", "text"]
          and body["model"] == llm.fast_model() and '"Hello"' in content[2]["text"] and body["temperature"] == 0,
          "one vision call: the product photo, the picture, the questions")
    cst = image_qa.cost()
    check(0.003 < cst["check_usd"] < 0.006 and abs(cst["retry_usd"] - (0.1071 + cst["check_usd"])) < 1e-4,
          f"the check is estimated at ${cst['check_usd']} a picture; a silent retry at ${cst['retry_usd']}")

    def make(versions=1, preset="shelf"):
        conf = dict(legacy, versions=versions, preset=preset)
        qq = post(c, "/app/create/quote", conf).get_json()
        rr = post(c, "/app/create/generate", dict(conf, total=qq["total"]))
        assert rr.status_code == 200, rr.get_json()
        with db.connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (WS,)).fetchone()["id"], qq["total"]

    def qa_rows(jid):
        with db.connect() as conn:
            return conn.execute("SELECT * FROM asset_qa WHERE job_id=? ORDER BY id", (jid,)).fetchall()

    os.environ["IMAGE_QA"] = "1"
    # (a) passes first time
    jid, price = make()
    bal = db.balance(ws_row(WS))
    answers.append(dict(GOOD, other_text=["Worth the shelf space."]))
    worker.tick()           # one pass submits it and reads the finished result (the fake vendor is instant)
    rows = qa_rows(jid)
    check(job_row(jid)["status"] == "done" and len(outs_of(jid)) == 1 and db.balance(ws_row(WS)) == bal
          and [(r["verdict"], r["attempt"], r["preset"], r["ad_type"]) for r in rows] == [("pass", 1, "shelf", "image")]
          and rows[0]["usd"] > 0 and json.loads(rows[0]["checks_json"])["label_matches"] is True,
          "a picture that passes is delivered; its row records pass, the preset and what the check cost")
    # (b) fails once, passes on the silent retry
    jid, price = make()
    bal = db.balance(ws_row(WS))
    answers.append(dict(BAD_LABEL))
    worker.tick()
    key1 = V.keys[-1]
    mid = job_row(jid)
    check(mid["status"] == "pending" and mid["request_id"] is None and outs_of(jid) == [] and mid["error"] is None
          and json.loads(mid["params_json"])["qa_attempt"] == 2 and db.balance(ws_row(WS)) == bal,
          "first failure: nothing is shown, nothing is refunded, the ad is quietly back in the queue")
    card = c.get(f"/app/create/batch/{mid['batch_id']}").get_data(as_text=True)
    check("couldn't get this one right" not in card and "In the queue" in card, "the member just sees it still being made")
    answers.append(dict(GOOD, other_text=["Worth the shelf space."]))
    worker.tick()
    check(V.keys[-1] and V.keys[-1] != key1 and V.submits[-1]["inputs"]["batch_size"] == 1,
          "the retry is a new vendor request (a new idempotency key), for the one failed picture")
    rows = qa_rows(jid)
    check(job_row(jid)["status"] == "done" and len(outs_of(jid)) == 1 and db.balance(ws_row(WS)) == bal
          and [(r["verdict"], r["attempt"]) for r in rows] == [("fail", 1), ("pass", 2)]
          and json.loads(rows[0]["reasons_json"]) == ["The label or logo doesn't match the product photo."]
          and not os.path.exists(rows[0]["path"]) and abs(job_row(jid)["cost_usd"] - 2 * 0.1071) < 1e-3,
          "second try passes: delivered at the price paid; the failed picture is gone and its reason is on record")
    # (c) fails twice: refunded automatically
    jid, price = make()
    bal = db.balance(ws_row(WS))
    answers.append(dict(GOOD, product_present=False))
    worker.tick()
    answers.append(dict(GOOD, deformed_hands=True))
    worker.tick()
    jf = job_row(jid)
    check(jf["status"] == "failed" and jf["error"] == image_qa.FAIL_TEXT == "We couldn't get this one right — credits returned."
          and db.balance(ws_row(WS)) == bal + price and outs_of(jid) == [],
          "second failure: the credits come back by themselves and the ad says \"We couldn't get this one right — credits returned.\"")
    import html
    card = html.unescape(c.get(f"/app/create/batch/{jf['batch_id']}").get_data(as_text=True))
    check("We couldn't get this one right — credits returned." in card and "credits returned. Credits refunded" not in card,
          "the card shows that message once, without a second 'Credits refunded.'")
    with db.connect() as conn:
        led = conn.execute("SELECT SUM(delta) FROM ledger WHERE job_id=?", (jid,)).fetchone()[0]
    check(led == 0 and [(r["verdict"], r["attempt"]) for r in qa_rows(jid)] == [("fail", 1), ("fail", 2)]
          and image_qa.reasons_for(db.connect().__enter__(), jid)[0]["reasons"] == ["A hand or a face is malformed."],
          "the ledger nets to zero for that ad, and both failures are on record with their reasons")
    import jobs_routes
    with db.connect() as conn:
        check(jobs_routes.price_now(conn, jf)[0] == price and json.loads(jf["params_json"])["qa_attempt"] == 2
              and jobs_routes.retryable(jf), "the refunded ad can be tried again at the normal price")
    r = c.post(f"/app/jobs/{jid}/retry", data={"csrf": csrf(c), "total": price}, headers={"Accept": "application/json"})
    rp = json.loads(job_row(r.get_json()["job"])["params_json"])
    check(r.status_code == 200 and "qa_attempt" not in rp and "qa_want" not in rp and rp["batch"] == 1,
          "a paid retry starts clean: every version again, with its own check and silent retry")
    answers.append(dict(GOOD, other_text=["Worth the shelf space."]))
    worker.tick()
    check(job_row(r.get_json()["job"])["status"] == "done", "and it is delivered when it passes")
    # (d) three versions: one fails twice -> two delivered, its share refunded
    jid, price = make(versions=3)
    bal = db.balance(ws_row(WS))
    answers.extend([dict(GOOD, other_text=["Worth the shelf space."]), dict(BAD_LABEL), dict(GOOD)])
    worker.tick()
    check(job_row(jid)["status"] == "pending" and outs_of(jid) == [] and len(image_qa.kept(db.connect().__enter__(), jid)) == 2,
          "3 versions, 1 fails: the 2 good ones are held back while the third is made again")
    answers.append(dict(BAD_LABEL))
    worker.tick()
    check(V.submits[-1]["inputs"]["batch_size"] == 1, "only the failed one is made again")
    j3 = job_row(jid)
    back = price - catalog.price_credits("ad_image", batch=2)
    check(j3["status"] == "done" and len(outs_of(jid)) == 2 and db.balance(ws_row(WS)) == bal + back and back > 0
          and j3["credits"] == price - back and "1 of 3 couldn't be made right" in j3["error"],
          f"it fails again: 2 pictures delivered, {back} credits (the third's share) returned")
    # (e) the check can't run: delivered unchecked
    jid, price = make()
    answers.append(llm.LLMError("529: overloaded"))
    worker.tick()
    rows = qa_rows(jid)
    check(job_row(jid)["status"] == "done" and len(outs_of(jid)) == 1 and rows[0]["verdict"] == "skipped",
          "when the check itself can't run the picture is delivered and the row says 'skipped'")
    os.environ["IMAGE_QA_DAILY_MAX"] = "0"
    jid, price = make()
    n_calls = len(model_calls)
    worker.tick()
    check(job_row(jid)["status"] == "done" and len(model_calls) == n_calls and qa_rows(jid)[0]["verdict"] == "skipped"
          and "budget" in qa_rows(jid)[0]["reasons_json"], "past IMAGE_QA_DAILY_MAX pictures are delivered without a check")
    os.environ.pop("IMAGE_QA_DAILY_MAX")
    # (f) a layered static: the check looks at the text-free picture and allows no words
    lq2 = post(c, "/app/create/quote", cfg).get_json()
    post(c, "/app/create/generate", dict(cfg, total=lq2["total"]))
    answers.append(dict(GOOD))
    worker.tick()
    txt = model_calls[-1]["messages"][0]["content"][2]["text"]
    with db.connect() as conn:
        jl = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (WS,)).fetchone()
    check("meant to carry no text at all" in txt and jl["status"] == "done"
          and os.path.exists(textlayer.base_path(outs_of(jl["id"])[0]["path"])),
          "a layered static is checked before its words are set: no text at all is allowed on it")
    acc_rows = image_qa.acceptance(db.connect().__enter__())
    shelf = next(a for a in acc_rows if a["preset"] == "shelf")
    check(shelf["first_pass"] == 4 and shelf["first_fail"] == 3 and shelf["retry_pass"] == 1 and shelf["refunded"] == 2
          and shelf["skipped"] == 2 and shelf["first_pass_rate"] == round(4 / 7, 3) and shelf["usd"] > 0,
          f"acceptance per preset: {shelf}")
    os.environ.pop("IMAGE_QA")

    # ------------------------------------------------------------------ 6. one free fix per delivered picture
    print("== one free fix per delivered asset")
    src_job = job_row(J1["id"])
    oid = outs_of(src_job["id"])[0]["id"]
    card = c.get(f"/app/create/batch/{src_job['batch_id']}").get_data(as_text=True)
    check("Fix it free" not in card and c.post(f"/app/jobs/{src_job['id']}/fix", data={"csrf": csrf(c), "note": "make it brighter"}).status_code == 404,
          "FREE_FIX off: no button, no route")
    os.environ["FREE_FIX"] = "1"
    card = c.get(f"/app/create/batch/{src_job['batch_id']}").get_data(as_text=True)
    check("Not right? Fix it free" in card and f"/app/jobs/{src_job['id']}/fix" in card and "10 of 10 left this month" in card,
          "FREE_FIX on: a delivered picture's card offers the fix and shows what's left this month")
    bal = db.balance(ws_row(WS))
    r = c.post(f"/app/jobs/{src_job['id']}/fix", data={"csrf": csrf(c), "output": oid, "note": "hi"}, headers={"Accept": "application/json"})
    check(r.status_code == 400 and "what to fix" in r.get_json()["error"], "a fix needs a note")
    r = c.post(f"/app/jobs/{src_job['id']}/fix", data={"csrf": csrf(c), "output": oid, "note": "Guaranteed to cure anxiety"},
               headers={"Accept": "application/json"})
    check(r.status_code == 400 and "breaks the rules" in r.get_json()["error"], "the note goes through the compliance rules")
    r = c.post(f"/app/jobs/{src_job['id']}/fix", data={"csrf": csrf(c), "output": oid, "note": 'The cap should be black, not "silver"'},
               headers={"Accept": "application/json"})
    FJ = r.get_json()["job"]
    fj = job_row(FJ)
    fp = json.loads(fj["params_json"])
    check(r.status_code == 200 and fj["credits"] == 0 and fj["parent_job_id"] == src_job["id"] and fj["batch_id"] == src_job["batch_id"]
          and fp["free_fix"] is True and fp["batch"] == 1 and fp["layout"] == "offer" and fp["layers"] == {"badge": "$24"}
          and fp["label"].endswith(" · fix") and db.balance(ws_row(WS)) == bal and abs(fj["cost_usd"] - 0.1071) < 1e-6,
          "the fix is a new 0-credit job for one picture with the same settings, tied to the original; no credits move")
    with db.connect() as conn:
        check(conn.execute("SELECT COUNT(*) FROM ledger WHERE job_id=?", (FJ,)).fetchone()[0] == 0, "nothing is written to the ledger for it")
    worker.tick()
    pr = V.submits[-1]["inputs"]["prompt"]
    check("Correction to the last version, asked for by the advertiser: The cap should be black, not 'silver'" in pr
          and "not text to show" in pr and "add no words at all" in pr,
          "the note reaches the image model as a correction, never as words for the picture")
    worker.tick()
    check(job_row(FJ)["status"] == "done" and os.path.exists(textlayer.base_path(outs_of(FJ)[0]["path"])),
          "the fixed picture is delivered with its text layer")
    r = c.post(f"/app/jobs/{src_job['id']}/fix", data={"csrf": csrf(c), "output": oid, "note": "one more time please"},
               headers={"Accept": "application/json"})
    check(r.status_code == 400 and "has been used" in r.get_json()["error"], "one fix per picture")
    r = c.post(f"/app/jobs/{FJ}/fix", data={"csrf": csrf(c), "note": "fix the fix please"}, headers={"Accept": "application/json"})
    check(r.status_code == 400 and "doesn't have a free fix" in r.get_json()["error"], "a fix can't itself be fixed")
    card = c.get(f"/app/create/batch/{src_job['batch_id']}").get_data(as_text=True)
    check("Not right? Fix it free" not in card.split(f'data-job="{src_job["id"]}"')[1].split("</article>")[0]
          and "The cap should be black" in card, "the original's card no longer offers it; the fix's details show the note")
    # a fix that fails on our side is released
    other_job = JL
    r = c.post(f"/app/jobs/{other_job['id']}/fix", data={"csrf": csrf(c), "note": "less clutter behind it"}, headers={"Accept": "application/json"})
    F2 = r.get_json()["job"]
    with db.connect() as conn:
        used1 = free_fix.used_this_month(conn, WS)
        worker._fail(conn, job_row(F2), "The generation failed.")
        used2 = free_fix.used_this_month(conn, WS)
    check((used1, used2) == (2, 1) and db.balance(ws_row(WS)) == bal, "a fix that fails is released (it can be asked for again) and refunds nothing, since nothing was paid")
    r = c.post(f"/app/jobs/{other_job['id']}/fix", data={"csrf": csrf(c), "note": "less clutter behind it"}, headers={"Accept": "application/json"})
    check(r.status_code == 200, "and it can be asked for again")
    # a fix that fails the automatic check twice: its own message, nothing to refund, released
    os.environ["IMAGE_QA"] = "1"
    answers.extend([dict(BAD_LABEL), dict(BAD_LABEL)])
    worker.tick()
    worker.tick()
    with db.connect() as conn:
        f3 = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (WS,)).fetchone()
        used3 = free_fix.used_this_month(conn, WS)
    check(f3["status"] == "failed" and f3["error"] == free_fix.FAIL_TEXT and used3 == 1 and db.balance(ws_row(WS)) == bal
          and not jobs_routes.retryable(f3),
          "a fix that fails the check twice says so, is released, moves no credits and can't be bought as a retry")
    os.environ.pop("IMAGE_QA")
    r = c.post(f"/app/jobs/{other_job['id']}/fix", data={"csrf": csrf(c), "note": "less clutter behind it"}, headers={"Accept": "application/json"})
    check(r.status_code == 200, "so it can be asked for once more")
    # the monthly cap: the setting, then the workspace's own
    os.environ["FREE_FIX_MONTHLY_CAP"] = "2"
    with db.connect() as conn:
        qa_job = conn.execute("SELECT j.* FROM jobs j JOIN outputs o ON o.job_id=j.id WHERE j.workspace_id=? AND j.status='done' "
                              "AND j.credits>0 AND j.id NOT IN (?, ?) ORDER BY j.id LIMIT 1", (WS, src_job["id"], other_job["id"])).fetchone()
    r = c.post(f"/app/jobs/{qa_job['id']}/fix", data={"csrf": csrf(c), "note": "warmer light please"}, headers={"Accept": "application/json"})
    check(r.status_code == 400 and "This month's 2 free fixes are used" in r.get_json()["error"], "FREE_FIX_MONTHLY_CAP=2: the third fix this month is refused")
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET free_fix_cap=5 WHERE id=?", (WS,))
        conn.commit()
        check(free_fix.cap(conn.execute("SELECT * FROM workspaces WHERE id=?", (WS,)).fetchone()) == 5, "a workspace's own cap overrides the setting")
    r = c.post(f"/app/jobs/{qa_job['id']}/fix", data={"csrf": csrf(c), "note": "warmer light please"}, headers={"Accept": "application/json"})
    check(r.status_code == 200, "with the workspace's cap at 5 the fix goes through")
    with db.connect() as conn:
        conn.execute("UPDATE asset_fixes SET created_at='2001-01-05T00:00:00+00:00' WHERE workspace_id=?", (WS,))
        conn.commit()
        check(free_fix.used_this_month(conn, WS) == 0, "last month's fixes don't count against this month")
    fc = free_fix.cost()
    check(fc["fix_usd"] == 0.1071 and fc["worst_usd"] == 0.2142 and "0.1071" in free_fix.__doc__ and "FREE_FIX_MONTHLY_CAP" in free_fix.__doc__,
          f"cost per fix is documented and computed: ${fc['fix_usd']} (${fc['worst_usd']} with a silent retry)")
    with db.connect() as conn:
        vid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                           "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (WS, u["id"], "photo_to_video", "x", "{}", "done", 463, 2.3,
                                                                         db.now(), db.now())).lastrowid
        conn.commit()
    check(free_fix.fixable(job_row(vid)) is False, "videos have no free fix yet (images only)")
    os.environ.pop("FREE_FIX")
    os.environ.pop("FREE_FIX_MONTHLY_CAP")

    # ------------------------------------------------------------------ 7. the brand kit's look
    print("== the brand kit: colours, font, logo")
    pg = c.get(f"/app/brands/{BID}/edit").get_data(as_text=True)
    check('name="kit_primary"' in pg and 'name="kit_font"' in pg and 'name="kit_logo"' in pg and "DM Serif Display" in pg,
          "the brand page has the colours, the font and the logo")
    r = c.post(f"/app/brands/{BID}/edit", data={"csrf": csrf(c), "brand_name": "Northwind", "kit_primary": "#112233",
                                                "kit_accent": "#FFAA00", "kit_font": "sans",
                                                "kit_logo": (io.BytesIO(png_bytes((200, 80), (10, 20, 30))), "logo.png")},
               content_type="multipart/form-data")
    with db.connect() as conn:
        br = conn.execute("SELECT * FROM brands WHERE id=?", (BID,)).fetchone()
    k = textlayer.kit_of(br)
    check(r.status_code == 302 and (k["primary"], k["accent"], k["font"]) == ("#112233", "#ffaa00", "sans")
          and k["logo"] and k["logo"] != logo and os.path.exists(k["logo"]) and c.get(f"/app/brands/{BID}/logo").status_code == 200,
          "saving it stores clean colours, the font and the uploaded logo, which the text layer then uses")

    # ------------------------------------------------------------------ 8. nothing left the machine
    print("== no vendor was really called")
    check(leaks == [] and answers == [], "no socket left the machine, and every faked model answer was used")
    check(V.uploads >= 1 and all(s_["model"] == "higgsfield/ads-studio/v1.0" for s_ in V.submits),
          f"the {len(V.submits)} generations and {V.uploads} upload(s) all went to the in-memory vendor; {len(model_calls)} model "
          "calls went to the fake")
    print(f"\n{passed} quality wave 2 checks passed")
finally:
    socket.socket.connect = _real_connect
    shutil.rmtree(TMP, ignore_errors=True)

"""The library tools' cheaper settings, the product a library job carries, and one-by-one downloads (m15), on a
throwaway database.

  * `video_seconds` and `video_resolution` for preset_videos: the default is the preset's own length at 720p and the
    same quote as before; a shorter clip or 480p is priced in credits and vendor dollars, the length is kept inside
    what the model takes (4 to 30 for a talking preset, 5 or 10 for a product video), a spoken example says a line
    that fits, and the worker sends exactly that length and size. Members still can't ask for 480p.
  * `voice_seconds` for presenter_voices: 4 to 8, default 6, priced at the 480p rate.
  * a presenter's still or voice sample carries no product (never the workspace's active one), also when it is
    retried; a preset's example carries its demo product; a packshot carries the demo product once that exists.
    Rows made before this are left as they were.
  * list_library_assets and the admin page give every file its output id (the one get_asset_download_url takes), the
    name the export zip gives it and whether the zip takes it; a file downloads from the page under that name.

No network: every vendor is faked, and a socket that tries to leave the machine fails the test.

    python tests/test_m15_library_options.py      (tests/test_app.py runs it too)
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
TMP = tempfile.mkdtemp(prefix="rs_m15opt_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("FIRST_RUN_NUDGES", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "RS_FREE_PREVIEW", "RS_TALKING", "RS_SERVICE_PATH",
          "CLAIMS_CHECK", "IMAGE_QA", "FREE_FIX", "TEXT_LAYERS", "RS_MARKUP", "RS_HD_MARKUP", "RS_TALKING_DURATIONS",
          "PRESENTER_STILLS", "PRESENTER_VOICE_REF", "RS_LIBRARY_STILL_MODEL", "RS_LIBRARY_STILL_USD",
          "RS_VIDEO_USD_PER_SECOND", "RS_TALKING_USD_PER_SECOND", "RS_VIDEO_USD_PER_SECOND_480P",
          "RS_VIDEO_USD_PER_SECOND_1080P", "RS_CARD_REQUIRED", "UI_V2", "RS_JOB_CANCEL"):
    os.environ.pop(k, None)
# The keys are made-up strings: nothing here may reach a vendor with them (see the socket guard below).
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_PUBLIC_URL="https://campaignify.test", RS_DISK_MIN_GB="0.2", ADS_READY_EMAIL="0", MCP_SERVER="on")

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
import library  # noqa: E402
import presenters  # noqa: E402
import presets  # noqa: E402
import products  # noqa: E402
import worker  # noqa: E402
from PIL import Image  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.32.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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
BASE = "http://localhost"
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


# ---------------------------------------------------------------- the fake vendor
class Vendor:
    def __init__(self):
        self.submits, self.uploads, self.n, self.files = [], [], 0, {}

    def upload(self, data, ctype):
        self.uploads.append({"bytes": len(data), "type": ctype})
        return f"https://files.example/up{len(self.uploads)}"

    def submit(self, model, inputs, webhook_url=None, idempotency_key=None):
        self.n += 1
        self.submits.append({"model": model, "inputs": inputs, "key": idempotency_key})
        return f"req-{self.n}"

    def status(self, rid):
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
hf.estimate = lambda model, inputs: None             # no live estimate: our own table prices everything
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


def quote(key, kind, ids, **options):
    return call(key, "quote_library_assets", {"kind": kind, "ids": ids, "options": options})


def make(key, kind, ids, **options):
    q = quote(key, kind, ids, **options)["structuredContent"]
    g = call(key, "generate_library_assets", {"kind": kind, "ids": ids, "options": options,
                                              "confirm_total_credits": q["total_credits"]})["structuredContent"]
    return q, g, {j["key"]: j["job_id"] for j in g["jobs"]}


RATE_480, RATE_720, RATE_1080 = 0.2056, 0.4622, 1.1372
try:
    adm, au = signup("owner@example.com", credits=100000, admin=True, plan="scale")
    mem, mu = signup("member@example.com", credits=5000)
    AWS, MWS = au["workspace_id"], mu["workspace_id"]
    photo_path = os.path.join(TMP, "product.png")
    open(photo_path, "wb").write(png_bytes((600, 600), (90, 110, 130)))

    def add_product(user, ws_id, brand_name, name):
        with appmod.app.test_request_context("/"):
            from flask import g
            with db.connect() as conn:
                g.user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
                pid = products.save_kit(conn, g.ws, brand_name=brand_name, product_name=name, audience="Homeowners", claims="")
                products.add_photo(conn, ws_id, pid, photo_path, "product.png", "image/png")
                conn.commit()
        return pid
    # the admin's own workspace has an ACTIVE product of its own, as a real one does
    ACTIVE = add_product(au, AWS, "House", "Roof Rejuvenation")
    PID = add_product(mu, MWS, "Northwind", "Trail Mug")
    check(ws_row(AWS)["product_id"] == ACTIVE, "(the admin's workspace has an active product of its own)")
    akey, mkey = make_key(adm, "write"), make_key(mem, "write")

    # a library row from before this change: tagged with the active product, and it stays that way
    with db.connect() as conn:
        t = db.now()
        OLD = conn.execute(
            "INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
            "updated_at, brand_id, product_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (AWS, au["id"], "library_still", "old", json.dumps({"library": {"kind": "presenter_still", "presenter": "walt",
                                                                             "situation": "shop-floor"}}),
             "failed", 0, 0.2, t, t, ws_row(AWS)["brand_id"], ACTIVE)).lastrowid
        conn.commit()

    # ------------------------------------------------------------------ 1. which product a library job carries
    print("== a presenter's still and voice carry no product; examples and packshots carry the demo product")
    _q, g1, sj = make(akey, "presenter_stills", ["maya:kitchen", "maya:sofa"])
    tick_until_idle()
    rows = [job_row(j) for j in sj.values()]
    check(g1["queued"] == 2 and all(r["status"] == "done" and r["product_id"] is None and r["brand_id"] is None for r in rows)
          and all(params_of(j)["no_product"] is True for j in sj.values()),
          "presenter stills carry no product and no brand, not the workspace's active product")
    la = call(akey, "list_assets", {})["structuredContent"]["assets"]
    still = next(a for a in la if a["job_id"] == sj["maya:kitchen"])
    check(still["product"] is None and still["library"] == {"kind": "presenter_still", "presenter": "maya",
                                                            "situation": "kitchen"}
          and still["outputs"][0]["filename"] == "presenter__maya__kitchen.png" and still["outputs"][0]["output_id"],
          "list_assets shows no product for a presenter still, and names its file as the export does")
    qv, gvoice, vj = make(akey, "presenter_voices", ["maya"])
    tick_until_idle()
    check(job_row(vj["maya"])["product_id"] is None and job_row(vj["maya"])["status"] == "done",
          "a voice sample carries no product")
    # a retry of a failed still keeps "no product" (the retry sends the same params)
    _q, _g, fj = make(akey, "presenter_stills", ["nia:desk"])
    with db.connect() as conn:
        worker._fail(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (fj["nia:desk"],)).fetchone(), "The generation failed.")
    r = adm.post(f"/app/jobs/{fj['nia:desk']}/retry", data={"csrf": csrf(adm), "total": "40"})
    with db.connect() as conn:
        again = conn.execute("SELECT * FROM jobs WHERE parent_job_id=?", (fj["nia:desk"],)).fetchone()
    check(r.status_code == 302 and again is not None and again["product_id"] is None and again["brand_id"] is None,
          "a retried still carries no product either")
    tick_until_idle()
    _q, _g, dj = make(akey, "demo_products", ["mug", "candle", "coffee"])
    tick_until_idle()
    check(job_row(dj["mug"])["product_id"] is None and job_row(dj["mug"])["status"] == "done",
          "a packshot made before its demo product exists carries no product (never the active one)")
    _q, _g, ej = make(akey, "preset_examples", ["offer"])
    with db.connect() as conn:
        mug = library.demo_product(conn, ws_row(AWS), "mug")
    check(mug is not None and job_row(ej["offer"])["product_id"] == mug["id"] and mug["id"] != ACTIVE
          and job_row(ej["offer"])["brand_id"] == mug["brand_id"],
          "a preset example carries its demo product")
    check(job_row(dj["mug"])["product_id"] == mug["id"] and job_row(dj["mug"])["brand_id"] == mug["brand_id"]
          and job_row(dj["candle"])["product_id"] is None,
          "setting the demo product up from its packshot gives that packshot's job the product; the others wait")
    _q, _g, dj2 = make(akey, "demo_products", ["mug"])
    check(job_row(dj2["mug"])["product_id"] == mug["id"] and "no_product" not in params_of(dj2["mug"]),
          "a packshot made again carries the demo product")
    tick_until_idle()
    la = {a["job_id"]: a for a in call(akey, "list_assets", {})["structuredContent"]["assets"]}
    check(la[dj2["mug"]]["product"] == "Dapplewood Trail Mug" and la[ej["offer"]]["product"] == "Dapplewood Trail Mug"
          and la[sj["maya:sofa"]]["product"] is None, "the listings say so")
    old = job_row(OLD)
    check(old["product_id"] == ACTIVE and la[OLD]["product"] == "Roof Rejuvenation", "a row made before this is left as it was")
    ml = call(mkey, "list_assets", {})["structuredContent"]
    check("library" not in json.dumps(ml), "(a member's list has no library rows)")

    # ------------------------------------------------------------------ 2. voice_seconds
    print("== voice_seconds: 4 to 8, default 6, at the 480p rate")
    credits = lambda usd: int(-(-round(usd * 2 * 100, 6) // 1))          # noqa: E731  (2x markup, rounded up)
    check(qv["total_credits"] == 247 == credits(RATE_480 * 6) and qv["vendor_usd"] == 1.23
          and qv["lines"][0]["seconds"] == 6 and qv["lines"][0]["resolution"] == "480p"
          and params_of(vj["maya"])["duration"] == 6,
          "the default is six seconds: 247 credits, $1.23 at the vendor, as before")
    for secs, want in ((4, 4), (5, 5), (8, 8), (7, 7)):
        q = quote(akey, "presenter_voices", ["maya"], voice_seconds=secs)["structuredContent"]
        if not (q["total_credits"] == credits(RATE_480 * want) and q["vendor_usd"] == round(RATE_480 * want, 2)
                and q["lines"][0]["seconds"] == want):
            check(False, f"voice_seconds={secs}: priced at {want} seconds ({q})")
    check(True, "4, 5, 7 and 8 seconds are each priced at their own length (165, 206, 288 and 329 credits)")
    check(credits(RATE_480 * 4) == 165 and credits(RATE_480 * 8) == 329, "(165 credits for 4 seconds, 329 for 8)")
    for bad in (3, 9, 60):
        res = quote(akey, "presenter_voices", ["maya"], voice_seconds=bad)
        check(res["isError"], f"the tool refuses voice_seconds={bad} (its schema says 4 to 8)")
    with appmod.app.test_request_context("/"), db.connect() as conn:
        from flask import g
        g.user, g.ws = au, ws_row(AWS)
        lo = library.quote(conn, g.ws, "presenter_voices", ["maya"], {"voice_seconds": "2"})
        hi = library.quote(conn, g.ws, "presenter_voices", ["maya"], {"voice_seconds": 30})
        try:
            library.quote(conn, g.ws, "presenter_voices", ["maya"], {"voice_seconds": "six"})
            word = ""
        except library.LibraryError as e:
            word = str(e)
    check(lo["lines"][0]["seconds"] == 4 and hi["lines"][0]["seconds"] == 8 and "whole number" in word,
          "typed on the page, a length outside 4 to 8 is moved to the nearest end; a word is refused")
    q4, g4, v4 = make(akey, "presenter_voices", ["maya"], voice_seconds=4)
    n_sub = len(V.submits)
    tick_until_idle()
    vs = V.submits[n_sub]["inputs"]
    check(g4["credits_charged"] == 165 and vs["duration"] == 4 and vs["resolution"] == "480p"
          and "Here's a quick look at this." in vs["prompt"] and "who it's for" not in vs["prompt"]
          and job_row(v4["maya"])["cost_usd"] == round(RATE_480 * 4, 4) and job_row(v4["maya"])["status"] == "done",
          "a four-second sample is sent at four seconds, 480p, with a line short enough to say in that time")
    check("who it's for" in presenters.voice_line("maya") == presenters.voice_line("maya", 6) == presenters.voice_line("maya", 8)
          and catalog.words(presenters.voice_line("maya", 4)) <= catalog.max_words(4),
          "six seconds and up keep the line they had")

    # ------------------------------------------------------------------ 3. video_seconds and video_resolution
    print("== video_seconds and video_resolution for preset videos")
    six = ["vo-product-shots", "hands-only-demo", "pov-unboxing", "talk-to-camera", "car-talk", "season-gift-clip"]
    base = quote(akey, "preset_videos", six, presenter="maya")["structuredContent"]
    check(base["total_credits"] == sum(presets.get(i).price_credits() for i in six) == 6381 and base["vendor_usd"] == 31.89
          and all("·" not in ln["label"].split("example")[1] for ln in base["lines"]),
          "with neither option the quote is what it was: 6,381 credits, $31.89")
    t0 = quote(akey, "preset_videos", ["talk-to-camera"], presenter="maya")["structuredContent"]
    check(t0["total_credits"] == 740 and t0["lines"][0]["seconds"] == 8 and t0["lines"][0]["resolution"] == "720p",
          "talk to camera by default: 8 seconds at 720p, 740 credits")
    cheap = quote(akey, "preset_videos", ["talk-to-camera"], presenter="maya", video_seconds=4,
                  video_resolution="480p")["structuredContent"]
    check(cheap["total_credits"] == 165 and cheap["vendor_usd"] == 0.82 and cheap["lines"][0]["seconds"] == 4
          and cheap["lines"][0]["resolution"] == "480p" and cheap["lines"][0]["label"].endswith("example · 4s · 480p"),
          "four seconds at 480p: 165 credits, $0.82 at the vendor, and the line says the length and size")
    for opts, secs, res, usd, cr in (
            ({"video_seconds": 4}, 4, "720p", RATE_720 * 4, 370),
            ({"video_resolution": "480p"}, 8, "480p", RATE_480 * 8, 329),
            ({"video_seconds": 6, "video_resolution": "1080p"}, 6, "1080p", RATE_1080 * 6, 956),
            ({"video_seconds": 2}, 4, "720p", RATE_720 * 4, 370),          # under the model's minimum
            ({"video_seconds": 45}, 30, "720p", RATE_720 * 30, 2774),      # over its maximum
            ({"video_seconds": 12}, 12, "720p", RATE_720 * 12, 1110)):     # a length members aren't offered
        q = quote(akey, "preset_videos", ["talk-to-camera"], presenter="maya", **opts)["structuredContent"]
        ln = q["lines"][0]
        if not (ln["seconds"] == secs and ln["resolution"] == res and q["total_credits"] == cr
                and abs(ln["usd"] - usd) < 0.0001):
            check(False, f"{opts}: {secs} s at {res} for {cr} credits (got {ln}, {q['total_credits']})")
    check(True, "the length is kept to 4 to 30 seconds for a talking preset, and each length and size has its own price "
                "(720p and 1080p from the members' table, 480p at $0.2056 a second)")
    for want, got in ((3, 5), (7, 5), (8, 10), (20, 10)):
        q = quote(akey, "preset_videos", ["turn"], video_seconds=want)["structuredContent"]
        if q["lines"][0]["seconds"] != got or q["total_credits"] != catalog.price_credits("photo_to_video", duration=got):
            check(False, f"a product-video preset asked for {want} s is made at {got} s ({q['lines'][0]})")
    check(True, "a product-video preset is 5 or 10 seconds: the nearest one is used")
    q = quote(akey, "preset_videos", ["how"], video_seconds=5, video_resolution="480p")["structuredContent"]
    check(q["total_credits"] == credits(RATE_480 * 5) == 206 and q["lines"][0]["seconds"] == 5,
          "the 10-second coffee how-to can be made at 5 seconds, 480p, for 206 credits")
    res = quote(akey, "preset_videos", ["talk-to-camera"], video_resolution="360p")
    check(res["isError"], "an unknown size is refused by the tool")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "quote", "kind": "preset_videos", "ids": "talk-to-camera",
                                         "video_resolution": "360p"})
    check("video_resolution is one of: 480p, 720p, 1080p" in r.get_data(as_text=True), "and by the page")
    # the quote is the same before the demo product is set up (candle: packshot made, product not yet) and after
    before = quote(akey, "preset_videos", ["pov-unboxing"], video_seconds=5, video_resolution="480p")["structuredContent"]
    with db.connect() as conn:
        check(library.demo_product(conn, ws_row(AWS), "candle") is None, "(the candle isn't a product yet)")
    qc, gc, cj = make(akey, "preset_videos", ["pov-unboxing"], video_seconds=5, video_resolution="480p")
    check(before["total_credits"] == qc["total_credits"] == gc["credits_charged"] == 206
          and before["lines"][0]["seconds"] == qc["lines"][0]["seconds"] == 5,
          "the quote is the same before and after the demo product is set up")
    with db.connect() as conn:
        candle = library.demo_product(conn, ws_row(AWS), "candle")
    check(job_row(cj["pov-unboxing"])["product_id"] == candle["id"] == job_row(dj["candle"])["product_id"],
          "a video example carries its demo product (and the candle's packshot job now does too)")
    tick_until_idle()
    bal = db.balance(ws_row(AWS))
    qt, gt, tj = make(akey, "preset_videos", ["talk-to-camera", "vo-product-shots"], presenter="maya", video_seconds=4,
                      video_resolution="480p")
    n_sub = len(V.submits)
    tick_until_idle()
    talk = next(s for s in V.submits[n_sub:] if "selfie-style" in s["inputs"]["prompt"])
    vo = next(s for s in V.submits[n_sub:] if "narrator" in s["inputs"]["prompt"])
    pt = params_of(tj["talk-to-camera"])
    check(qt["total_credits"] == 330 == bal - db.balance(ws_row(AWS)) and gt["queued"] == 2
          and talk["inputs"]["duration"] == 4 == vo["inputs"]["duration"]
          and talk["inputs"]["resolution"] == "480p" == vo["inputs"]["resolution"]
          and pt["duration"] == 4 and pt["resolution"] == "480p" and pt["preset"] == "talk-to-camera"
          and pt["library"] == {"kind": "preset_video", "preset": "talk-to-camera", "demo": "mug"}
          and job_row(tj["talk-to-camera"])["cost_usd"] == round(RATE_480 * 4, 4)
          and job_row(tj["talk-to-camera"])["credits"] == 165
          and all(job_row(j)["status"] == "done" for j in tj.values()),
          "two examples at 4 seconds, 480p: 330 credits charged, and the worker sends that length and size")
    check('"Quick one about the Dapplewood Trail Mug."' in talk["inputs"]["prompt"] and "twelve ounces" not in talk["inputs"]["prompt"]
          and '"Meet the Dapplewood Trail Mug. Find it at Dapplewood."' in vo["inputs"]["prompt"]
          and job_row(tj["talk-to-camera"])["brief"] == "Quick one about the Dapplewood Trail Mug.",
          "a four-second example says its short line, whole, instead of a long script rushed")
    for pid, (_demo, script) in library._SCRIPTS.items():
        if not script:
            continue
        own = presets.get(pid).price["duration"]
        for secs in range(4, 31):
            said = library.example_script(pid, secs)
            if catalog.words(said) > catalog.max_words(secs) or not said or (secs >= own and said != script):
                check(False, f"{pid} at {secs} s says {said!r}")
    check(True, "every example's words fit every length from 4 to 30 seconds, and its own length keeps its own script")
    _q, _g, lj = make(akey, "preset_videos", ["talk-to-camera"], presenter="maya", video_seconds=12)
    check(job_row(lj["talk-to-camera"])["brief"] == library._SCRIPTS["talk-to-camera"][1]
          and params_of(lj["talk-to-camera"])["duration"] == 12, "a longer example keeps its script")
    with db.connect() as conn:
        worker._fail(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (lj["talk-to-camera"],)).fetchone(), worker.CANCELED)
    # members: nothing changed
    r = post(mem, "/app/create/quote", {"product": PID, "type": "ugc", "items": ["Meet the mug."], "presenters": ["maya"],
                                        "duration": 8, "resolution": "480p"}).get_json()
    r2 = post(mem, "/app/create/quote", {"product": PID, "type": "ugc", "items": ["Meet the mug."], "presenters": ["maya"],
                                         "duration": 4}).get_json()
    r3 = post(mem, "/app/create/quote", {"product": PID, "type": "ugc", "items": ["Meet the mug."], "presenters": ["maya"],
                                         "duration": 8}).get_json()
    check("720p or 1080p" in r["error"] and "length" in r2["error"] and r3["total"] == 740 and "480p" not in catalog.resolutions("ugc_video")
          and 4 not in catalog.TOOLS["ugc_video"]["durations"],
          "members still can't ask for 480p or a 4-second video, and their price is unchanged")
    tools = {t["name"]: t for t in rpc("tools/list", {}, key=akey).get_json()["result"]["tools"]}
    opts = tools["quote_library_assets"]["inputSchema"]["properties"]["options"]["properties"]
    check({"video_seconds", "video_resolution", "voice_seconds"} <= set(opts) and opts["video_resolution"]["enum"] ==
          ["480p", "720p", "1080p"] and (opts["voice_seconds"]["minimum"], opts["voice_seconds"]["maximum"]) == (4, 8)
          and "480p" not in tools["quote_ad_batch"]["inputSchema"]["properties"]["resolution"]["enum"]
          and set(tools["generate_library_assets"]["inputSchema"]["properties"]["options"]["properties"]) == set(opts),
          "the admin tools take the three options; the members' tools don't know 480p")

    # ------------------------------------------------------------------ 4. files one by one
    print("== every file: its output id, its export name, and a download under that name")
    lst = call(akey, "list_library_assets", {"limit": 100})["structuredContent"]
    by_job = {a["job_id"]: a for a in lst["assets"]}
    a = by_job[sj["maya:kitchen"]]
    f = a["outputs"][0]
    check(a["output_id"] == f["output_id"] and a["filename"] == f["filename"] == "presenter__maya__kitchen.png"
          and a["in_export"] is True and f["download_url"].startswith(BASE + "/mcp/file/")
          and "get_asset_download_url" in lst["note"],
          "list_library_assets: the file's output id and export name, flat and per file, and whether the zip takes it")
    link = call(akey, "get_asset_download_url", {"output_id": a["output_id"]})["structuredContent"]
    got = anon.get(link["url"].replace(BASE, ""))
    check(link["output_id"] == a["output_id"] and got.status_code == 200 and got.data[:4] == b"\x89PNG",
          "that output id works with get_asset_download_url, and the link serves the file")
    check(by_job[tj["talk-to-camera"]]["filename"] == "preset__talk-to-camera.mp4"
          and by_job[v4["maya"]]["filename"] == "voice__maya.mp4" and by_job[dj2["mug"]]["filename"] == "demo__mug.png"
          and by_job[v4["maya"]]["in_export"] and not by_job[vj["maya"]]["in_export"]
          and by_job[dj2["mug"]]["in_export"] and not by_job[dj["mug"]]["in_export"]
          and fj["nia:desk"] not in by_job and by_job[OLD]["output_id"] is None and by_job[OLD]["outputs"] == [],
          "clips, voice samples and packshots are named by their tags; of two for one tag the zip takes the newest")
    z = zipfile.ZipFile(io.BytesIO(adm.get("/admin/library/export.zip").data))
    manifest = {m["file"]: m["job_id"] for m in json.loads(z.read("manifest.json"))}
    listed = {x["filename"]: x["job_id"] for x in lst["assets"] if x["in_export"]}
    check(manifest == listed and len(manifest) >= 6, "the files marked in_export are exactly the zip's, under the same names")
    # a job with two files: the second has its own name
    with db.connect() as conn:
        first = conn.execute("SELECT * FROM outputs WHERE id=?", (a["output_id"],)).fetchone()
        second = os.path.join(os.path.dirname(first["path"]), "second.png")
        shutil.copyfile(first["path"], second)
        OID2 = conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                            (sj["maya:kitchen"], "image", "", second)).lastrowid
        MEMBER_OUT = conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                                  (conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, params_json, status, "
                                                "credits, cost_usd, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                                (AWS, au["id"], "ad_image", "{}", "done", 22, 0.1, db.now(), db.now())).lastrowid,
                                   "image", "", second)).lastrowid
        conn.commit()
    two = next(x for x in call(akey, "list_library_assets", {"kind": "presenter_stills"})["structuredContent"]["assets"]
               if x["job_id"] == sj["maya:kitchen"])
    check([o["filename"] for o in two["outputs"]] == ["presenter__maya__kitchen.png", "presenter__maya__kitchen__2.png"]
          and [o["output_id"] for o in two["outputs"]] == [a["output_id"], OID2] and two["output_id"] == a["output_id"],
          "a job with two files lists both, each with its own id and name")
    r = adm.get(f"/admin/library/file/{a['output_id']}")
    r2 = adm.get(f"/admin/library/file/{OID2}")
    check(r.status_code == 200 and 'filename=presenter__maya__kitchen.png' in r.headers["Content-Disposition"]
          and r.headers["Content-Disposition"].startswith("attachment") and r.data[:4] == b"\x89PNG"
          and "presenter__maya__kitchen__2.png" in r2.headers["Content-Disposition"],
          "the page's download gives the file under its export name")
    check(adm.get(f"/admin/library/file/{MEMBER_OUT}").status_code == 404 and adm.get("/admin/library/file/999999").status_code == 404
          and mem.get(f"/admin/library/file/{a['output_id']}").status_code == 403
          and client().get(f"/admin/library/file/{a['output_id']}").status_code == 302,
          "only a library file of the admin's own workspace: 404 for anything else, 403 for a member")
    page = adm.get("/admin/library").get_data(as_text=True)
    check(f"/admin/library/file/{a['output_id']}" in page and f"output {a['output_id']}" in page
          and "presenter__maya__kitchen__2.png" in page and "in export" in page and "get_asset_download_url" in page
          and 'name="video_seconds"' in page and 'name="video_resolution"' in page and 'name="voice_seconds"' in page
          and "480p (library only)" in page,
          "the page lists every file with its output id and export name, and has the three new fields")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "quote", "kind": "preset_videos", "ids": "talk-to-camera",
                                         "presenter": "maya", "video_seconds": "4", "video_resolution": "480p"})
    html = r.get_data(as_text=True)
    check("1 job: 165 credits, about $0.82 at the vendor" in html and "4 s · 480p" in html and 'name="total" value="165"' in html
          and 'name="video_seconds"' in html and 'value="4"' in html and '<option value="480p" selected>' in html,
          "the page quotes with them and keeps them in the form for Generate")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "generate", "kind": "preset_videos", "ids": "talk-to-camera",
                                         "presenter": "maya", "video_seconds": "4", "video_resolution": "480p", "total": "740"})
    check("Quote it again" in r.get_data(as_text=True), "Generate with the full-length total is refused: the options are part of the price")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "generate", "kind": "preset_videos", "ids": "talk-to-camera",
                                         "presenter": "maya", "video_seconds": "4", "video_resolution": "480p", "total": "165"})
    check(r.status_code == 302, "with the quoted total it starts")
    r = adm.post("/admin/library", data={"csrf": csrf(adm), "do": "quote", "kind": "presenter_voices", "ids": "maya",
                                         "voice_seconds": "5"})
    check("1 job: 206 credits" in r.get_data(as_text=True) and "5 s · 480p" in r.get_data(as_text=True),
          "and a five-second voice sample is quoted from the page")
    tick_until_idle()

    print("== no vendor was really called")
    check(leaks == [], "no connection left this machine during the whole suite")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} m15 library option checks passed")

"""Clone an ad (adclone.py, adclone_routes.py) on a throwaway database: a real short video made with ffmpeg, the
models faked.

  * the page: the price on the button; no product, no clone
  * a video in: frames read, the model sees them as images, the beats come back cleaned, the remake is planned by
    Studio's Ask from the beats (with only the product's facts), and the run is settled on the credit hold
  * an image ad works too (one frame)
  * links: social posts refused with a plain reason; a non-file refused
  * the remake opens on the desk as a priced plan, once; nothing is made
  * a failed read gives the hold back; another workspace can't see the frames

    python tests/test_adclone.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_clone_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test", UI_V2="1",
                  ANTHROPIC_API_KEY="test")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "GROQ_API_KEY", "STRIPE_SECRET_KEY",
          "PLANS_ONLY", "MCP_SERVER"):
    os.environ.pop(k, None)

import adclone  # noqa: E402
import app as appmod  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import motion_edit  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


calls = []


def fake_structured(**kw):
    calls.append(kw)
    if kw.get("images"):
        if kw.get("usage") is not None:
            kw["usage"].add(llm.model(), 9000, 1200)
        return {"format": "presenter", "summary": "A creator holds the blender and makes a smoothie at the pool.",
                "length_s": 6, "pacing": "Fast cuts", "tone": "Upbeat",
                "beats": [{"beat": "Hook", "start_s": 0, "end_s": 1.2, "frame": 0, "shows": "Blender by the pool",
                           "says": "This changed my mornings", "why": "Shows the product in the first second"},
                          {"beat": "Problem", "start_s": 1.2, "end_s": 2.4, "frame": 2, "shows": "A bulky blender",
                           "says": "", "why": "Names the pain"},
                          {"beat": "Demo", "frame": 4, "shows": "Blending fruit", "why": "Proves it is easy"},
                          {"beat": "Shout", "frame": 99, "shows": "x", "why": "y"},
                          {"beat": "CTA", "start_s": 5, "end_s": 6, "frame": 7, "shows": "Logo and link",
                           "says": "Get yours", "why": "Tells you what to do"}]}
    return {"note": "One presenter video on the same beats, and two image ads on the hook.",
            "parts": [{"make": "presenter", "text": "Lighting a candle at night? Burns for 40 hours, hand poured in Texas. "
                                                   "Get yours today."},
                      {"make": "image", "text": "Your evening, in one candle", "count": 2}]}


llm.structured = fake_structured
llm.configured = lambda: True
motion_edit.has_whisper = lambda: False

_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.31.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (400, 500), (220, 90, 90)).save(b, "PNG")
    b.seek(0)
    return b


def member(email, with_product=True):
    import onboarding
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        onboarding.finish(conn, u)
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        db.adjust_credits(conn, u["workspace_id"], 5000, "test top-up")
        conn.commit()
    if with_product:
        c.post("/app/products/new", data={"csrf": csrf(c), "name": "Ember Candle", "photos": (png(), "p.png")},
               content_type="multipart/form-data")
        with db.connect() as conn:
            conn.execute("UPDATE products SET claims=?, description=? WHERE workspace_id=?",
                         ("Burns for 40 hours\nHand poured in Texas", "A soy wax candle.", u["workspace_id"]))
            conn.commit()
    return c, u["workspace_id"]


def wait(cid, secs=60):
    end = time.time() + secs
    while time.time() < end:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM ad_clones WHERE id=?", (cid,)).fetchone()
        if row["status"] in ("done", "failed"):
            return row
        time.sleep(0.2)
    raise SystemExit("FAIL: the clone never finished")


def bal(ws_id):
    with db.connect() as conn:
        return db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone())


VIDEO = os.path.join(TMP, "ref.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=24:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest", "-c:v", "libx264", "-pix_fmt",
                "yuv420p", "-c:a", "aac", VIDEO], check=True)

print("== the page")
c, ws = member("ana@example.com")
html = c.get("/app/clone").get_data(as_text=True)
check(f"about {adclone.quote():,} credits" in html, "the price is on the button")
check("Clone an ad" in c.get("/app/tools").get_data(as_text=True), "listed on Tools")
nc, _ = member("noprod@example.com", with_product=False)
r = nc.post("/app/clone", data={"csrf": csrf(nc), "file": (open(VIDEO, "rb"), "ref.mp4")}, content_type="multipart/form-data")
check(r.status_code == 302 and "/app/clone" in r.headers["Location"], "no product: back to the page, nothing read")

print("== a video in")
before = bal(ws)
r = c.post("/app/clone", data={"csrf": csrf(c), "file": (open(VIDEO, "rb"), "ref.mp4")}, content_type="multipart/form-data")
check(r.status_code == 302 and "/app/clone/" in r.headers["Location"], "uploaded, on to its page")
cid = int(r.headers["Location"].rstrip("/").split("/")[-1])
row = wait(cid)
check(row["status"] == "done", f"read ({row['error']})")
look = [k for k in calls if k.get("images")]
check(look and len(look[0]["images"]) == adclone.FRAMES and look[0]["images"][0][1] == "image/jpeg",
      "the model saw the frames as images")
a = json.loads(row["analysis_json"])
check([b["beat"] for b in a["beats"]] == ["Hook", "Problem", "Demo", "Other", "CTA"], "beats cleaned (unknown -> Other)")
check(all(0 <= b["frame"] < adclone.FRAMES for b in a["beats"]), "frame numbers kept in range")
plan_call = [k for k in calls if not k.get("images")][-1]
check("Never reuse the reference's brand" in plan_call["prompt"] and "Burns for 40 hours" in plan_call["prompt"],
      "the remake is asked of Studio's Ask with the product's own claims")
rm = json.loads(row["remake_json"])
check([i["make"] for i in rm["items"]] == ["presenter", "image"], "the remake: a presenter video and image ads")
check(0 < row["credits"] <= adclone.quote() and bal(ws) == before - row["credits"], "charged what it used, from the hold")
page = c.get(f"/app/clone/{cid}").get_data(as_text=True)
check("The ad, beat by beat" in page and "Hook" in page and "Open it on the desk" in page, "the page shows beats and the remake")
fr = json.loads(row["frames_json"])
check(c.get(f"/app/clone/{cid}/f/{fr[0]['file']}").status_code == 200, "a frame is served to its workspace")
other, _ = member("ben@example.com")
check(other.get(f"/app/clone/{cid}/f/{fr[0]['file']}").status_code == 404, "and not to another one")
check(other.get(f"/app/clone/{cid}").status_code == 404, "nor its page")
check(c.get(f"/app/clone/{cid}/f/..%2Fref.mp4").status_code == 404, "only frame names")

print("== on to the desk")
jobs_before = 0
with db.connect() as conn:
    jobs_before = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (ws,)).fetchone()[0]
r = c.post(f"/app/clone/{cid}/desk", data={"csrf": csrf(c)})
check(r.status_code == 302 and "/app/desk/c/" in r.headers["Location"], "opens a campaign on the desk")
with db.connect() as conn:
    tid = conn.execute("SELECT thread_id FROM ad_clones WHERE id=?", (cid,)).fetchone()[0]
    turn = conn.execute("SELECT body_json FROM desk_turns WHERE thread_id=? AND kind='plan'", (tid,)).fetchone()
    jobs_after = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (ws,)).fetchone()[0]
body = json.loads(turn["body_json"])
kinds = [p["kind"] for p in body["parts"]]
check("presenter" in kinds and "image" in kinds, "the plan holds the remake's parts")
made = [r for r in body["rows"] if r["stage"] == "make"]
check(made and all(r["label"].endswith("credits") for r in made), "each priced")
check(jobs_after == jobs_before, "nothing is made")
r2 = c.post(f"/app/clone/{cid}/desk", data={"csrf": csrf(c)})
check(r2.headers["Location"].startswith(f"/app/desk/c/{tid}"), "a second press opens the same campaign")

print("== an image ad")
r = c.post("/app/clone", data={"csrf": csrf(c), "file": (png(), "ad.png")}, content_type="multipart/form-data")
cid2 = int(r.headers["Location"].rstrip("/").split("/")[-1])
row2 = wait(cid2)
check(row2["status"] == "done" and len(json.loads(row2["frames_json"])) == 1, "one frame, read")

print("== links")
r = c.post("/app/clone", data={"csrf": csrf(c), "url": "https://www.tiktok.com/@x/video/1"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/clone"), "a TikTok link is refused")
with c.session_transaction() as s:
    flashes = s.get("_flashes") or []
check(any("save the video" in m.lower() for _, m in flashes), "with a plain reason")

print("== a failed read")
bad = os.path.join(TMP, "bad.mp4")
open(bad, "wb").write(b"\x00" * 2048)
before = bal(ws)
r = c.post("/app/clone", data={"csrf": csrf(c), "file": (open(bad, "rb"), "bad.mp4")}, content_type="multipart/form-data")
cid3 = int(r.headers["Location"].rstrip("/").split("/")[-1])
row3 = wait(cid3)
check(row3["status"] == "failed" and row3["error"], "a broken file fails with a reason")
check(bal(ws) == before, "and nothing is charged")

print(f"\nall {passed} clone checks passed")

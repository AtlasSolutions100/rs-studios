"""A product's screen recording (m38, recordings.py) on a throwaway database.

  * what is kept: a real MP4/MOV or WebM with a picture, 3 seconds to 3 minutes, no side over 4096 pixels. The
    container is read from the file's own first bytes and ffprobe is told it outright, so a playlist or a script
    labelled video/mp4 is refused and never opened as what it says it is
  * one per product: a new upload replaces the last, file and row; removing it removes both
  * only its owner: another workspace can't add one to the product, see it or remove it
  * the product page offers it for an app (or wherever one already is), with its length and size
  * how much of it a video shows: as it is when it fits its beats, up to twice as fast when it is longer, its
    start alone when it is longer still (recordings.window)
  * it counts toward the workspace's storage and goes, file and row, when the workspace is deleted
  * the screen layouts (stitch.py, real ffmpeg): a wide recording or screenshot runs across the top with the
    presenter below when the recipe asks for "split"; a tall phone screen keeps the corner layout; a recording
    plays from the second asked for at the speed asked for, and holds its last picture when it runs out

No network. The checks that read or render video need ffmpeg and ffprobe; without them they are skipped and the
suite says so.

    python tests/test_recordings.py      (tests/test_app.py runs it too)
"""
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_rec_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("RS_CARD_REQUIRED", "UI_V2", "PLANS_ONLY", "RS_EMAIL_VERIFY", "KEYFRAME_VIDEO", "FIRST_RUN_NUDGES"):
    os.environ.pop(k, None)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_PUBLIC_URL="https://campaignify.test", RS_DISK_MIN_GB="0.2", ADS_READY_EMAIL="0")

_real_connect = socket.socket.connect


def _guard_connect(self, address, *a, **k):
    host = address[0] if isinstance(address, tuple) else str(address)
    if isinstance(address, tuple) and host not in ("127.0.0.1", "::1", "localhost"):
        raise AssertionError(f"a test tried to reach the network: {address}")
    return _real_connect(self, address, *a, **k)


socket.socket.connect = _guard_connect

from PIL import Image  # noqa: E402

import app as appmod  # noqa: E402
import db  # noqa: E402
import products  # noqa: E402
import recordings  # noqa: E402
import stitch  # noqa: E402
import storage  # noqa: E402
import wsdelete  # noqa: E402

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
passed = 0


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
    c.environ_base["REMOTE_ADDR"] = f"10.38.0.{_ip[0] % 250 + 1}"
    c.environ_base["HTTP_USER_AGENT"] = UA
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (u["workspace_id"],))
        conn.commit()
    return c, u


def add_product(user, name, category):
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (user["workspace_id"],)).fetchone()
        pid = products.create_product(conn, ws, products.ensure_brand(conn, ws["id"]), {"name": name, "category": category})
        conn.commit()
    return pid


def video(name, size, seconds, src="testsrc", fmt=None):
    """A small real video file made here; its bytes."""
    path = os.path.join(TMP, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}{':' if '=' in src else '='}s={size[0]}x{size[1]}:r=24:d={seconds}",
                    "-pix_fmt", "yuv420p", *(["-f", fmt] if fmt else []), path], check=True)
    return open(path, "rb").read()


def upload(c, pid, data, filename="screen.mp4", ctype="video/mp4", follow=True):
    return c.post(f"/app/products/{pid}/recording", data={"csrf": csrf(c), "recording": (io.BytesIO(data), filename, ctype)},
                  content_type="multipart/form-data", follow_redirects=follow)


def rows(pid=None):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM recordings" + (" WHERE product_id=?" if pid else "") + " ORDER BY id",
                            (pid,) if pid else ()).fetchall()


def frame_px(path, t, xy):
    out = os.path.join(TMP, "px.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", out], check=True)
    with Image.open(out) as im:
        return im.convert("RGB").getpixel(xy)


def near(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


try:
    mem, mu = signup("member@example.com")
    oth, ou = signup("other@example.com")
    MWS, OWS = mu["workspace_id"], ou["workspace_id"]
    APP = add_product(mu, "Northwind Planner", "app_service")
    MUG = add_product(mu, "Trail Mug", "home")
    THEIRS = add_product(ou, "Their App", "app_service")

    # ------------------------------------------------------------------ 1. what the file is
    print("== the container is read from the file itself")
    for name, head, want in (("a.mp4", b"\x00\x00\x00\x18ftypmp42", "mov"), ("b.mov", b"\x00\x00\x00\x14ftypqt  ", "mov"),
                             ("c.mov", b"\x00\x00\x00\x08wide\x00\x00", "mov"), ("d.webm", b"\x1a\x45\xdf\xa3\x01\x00", "matroska"),
                             ("e.mp4", b"#EXTM3U\n#EXT-X-", ""), ("f.mp4", b"ffconcat version 1.0\n", ""),
                             ("g.mp4", b"<html><body>", ""), ("h.mp4", b"", "")):
        p = os.path.join(TMP, name)
        open(p, "wb").write(head + b"0" * 64 if head else b"")
        if recordings.container(p) != want:
            check(False, f"{name}: {recordings.container(p)!r}, wanted {want!r}")
    check(recordings.container(os.path.join(TMP, "nothing-here.mp4")) == "",
          "MP4, MOV and WebM by their first bytes; a playlist, a concat script, a page, an empty file and a missing "
          "one are none of them, whatever they are called")
    check(recordings.window(8, 8) == (1.0, 8.0) and recordings.window(5, 8) == (1.0, 5.0)
          and recordings.window(12, 8) == (1.5, 12.0) and recordings.window(16, 8) == (2.0, 16.0)
          and recordings.window(90, 8) == (2.0, 16.0) and recordings.window(0, 8) == (1.0, 0.0)
          and recordings.MAX_SPEED == 2.0,
          "a recording plays as it is when it fits its beats, up to twice as fast when it is longer, and past that "
          "only its start is shown")

    # ------------------------------------------------------------------ 2. adding, replacing, removing
    print("== one recording per product")
    r = mem.get(f"/app/products/{APP}")
    page = r.get_data(as_text=True)
    check(r.status_code == 200 and 'id="recording"' in page and "Screen recording" in page
          and f'action="/app/products/{APP}/recording"' in page and 'class="rec-view"' not in page
          and 'accept="video/mp4,video/quicktime,video/webm"' in page,
          "an app's product page offers the screen recording, before there is one")
    r = mem.get(f"/app/products/{MUG}")
    check(r.status_code == 200 and 'id="recording"' not in r.get_data(as_text=True),
          "a physical product's page doesn't mention it")
    r = upload(mem, APP, b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n")
    check(r.status_code == 200 and "isn&#39;t a video we can read" in r.get_data(as_text=True) and not rows()
          and not [f for f in os.listdir(os.path.join(TMP, "uploads", str(MWS))) if f.endswith(".mp4")]
          if os.path.isdir(os.path.join(TMP, "uploads", str(MWS))) else not rows(),
          "a playlist labelled video/mp4 is refused, nothing is kept, and it is never opened as a playlist")
    r = upload(mem, APP, b"\x89PNG\r\n\x1a\n" + b"0" * 200, "shot.png", "image/png")
    check("file type isn&#39;t supported" in r.get_data(as_text=True) and not rows(), "a picture isn't a recording")
    r = mem.post(f"/app/products/{APP}/recording", data={"csrf": csrf(mem)}, follow_redirects=True)
    check("Pick a screen recording to add." in r.get_data(as_text=True), "no file: said so")
    if FFMPEG:
        wide = video("wide.mp4", (640, 360), 6)
        short = video("short.mp4", (640, 360), 1.5)
        r = upload(mem, APP, short)
        check("under 3 seconds" in r.get_data(as_text=True) and not rows(), "under three seconds: refused, with what to do")
        r = upload(mem, APP, wide, follow=False)
        got = rows(APP)
        check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/products/{APP}#recording") and len(got) == 1
              and got[0]["workspace_id"] == MWS and (got[0]["width"], got[0]["height"]) == (640, 360)
              and abs(got[0]["seconds"] - 6.0) < 0.2 and os.path.exists(got[0]["path"])
              and os.path.dirname(got[0]["path"]) == os.path.join(TMP, "uploads", str(MWS))
              and got[0]["content_type"] == "video/mp4" and got[0]["filename"] == "screen.mp4",
              "a real recording is kept in the workspace's uploads, with its length and size as shown")
        first = got[0]
        with db.connect() as conn:
            check(recordings.of_product(conn, MWS, APP)["id"] == first["id"] and recordings.of_product(conn, MWS, MUG) is None
                  and recordings.of_product(conn, OWS, APP) is None and recordings.get(conn, OWS, first["id"]) is None
                  and recordings.get(conn, MWS, first["id"])["id"] == first["id"]
                  and recordings.summary(first) == "0:06, 640 x 360",
                  "it is its product's, in its workspace only")
            check(storage.used_bytes(MWS, conn) >= len(wide), "and counts toward the workspace's storage")
        page = mem.get(f"/app/products/{APP}").get_data(as_text=True)
        check(f'src="/app/recordings/{first["id"]}"' in page and "0:06, 640 x 360" in page and ">Replace<" in page
              and 'value="remove"' in page, "the page plays it back and offers Replace and Remove")
        r = mem.get(f"/app/recordings/{first['id']}")
        check(r.status_code == 200 and r.mimetype == "video/mp4" and r.get_data() == wide, "its owner can play it")
        r.close()
        check(oth.get(f"/app/recordings/{first['id']}").status_code == 404
              and upload(oth, APP, wide, follow=False).status_code == 404
              and oth.post(f"/app/products/{APP}/recording", data={"csrf": csrf(oth), "do": "remove"}).status_code == 404
              and len(rows(APP)) == 1 and client().get(f"/app/recordings/{first['id']}").status_code in (302, 401),
              "another workspace can't see it, add one to this product or remove it; a visitor is sent to log in")
        webm = video("phone.webm", (360, 780), 4)
        r = upload(mem, APP, webm, "phone.webm", "video/webm", follow=False)
        got = rows(APP)
        check(r.status_code == 302 and len(got) == 1 and got[0]["id"] != first["id"] and not os.path.exists(first["path"])
              and (got[0]["width"], got[0]["height"]) == (360, 780) and recordings.container(got[0]["path"]) == "matroska",
              "a new upload replaces the last: one row, the old file gone (a WebM this time)")
        r = mem.post(f"/app/products/{APP}/recording", data={"csrf": csrf(mem), "do": "remove"}, follow_redirects=True)
        check("Removed the screen recording." in r.get_data(as_text=True) and not rows() and not os.path.exists(got[0]["path"]),
              "Remove takes the row and the file")
        # a file that vanished from disk counts as no recording
        upload(mem, APP, wide)
        gone = rows(APP)[0]
        os.remove(gone["path"])
        with db.connect() as conn:
            check(recordings.of_product(conn, MWS, APP) is None and recordings.get(conn, MWS, gone["id"]) is None,
                  "a recording whose file is gone counts as none")
        upload(mem, APP, wide)

        # -------------------------------------------------------------- 3. deleting the workspace
        print("== it goes with the workspace")
        upload(oth, THEIRS, wide)
        theirs = rows(THEIRS)[0]
        with db.connect() as conn:
            wsdelete.ensure_table(conn)
            wsdelete.purge(conn, conn.execute("SELECT * FROM workspaces WHERE id=?", (OWS,)).fetchone())
        check(not rows(THEIRS) and not os.path.exists(theirs["path"]) and len(rows(APP)) == 1
              and os.path.exists(rows(APP)[0]["path"]),
              "deleting a workspace removes its recording, row and file, and nobody else's")

        # -------------------------------------------------------------- 4. the layouts
        print("== a screen across the top, or the presenter in the corner")
        size, fps = (180, 320), 24
        presenter = os.path.join(TMP, "presenter.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xc81e1e:s=180x320:r=24:d=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest", "-pix_fmt", "yuv420p",
                        presenter], check=True)
        # a recording that is blue for its first 4 seconds and green after
        rec = os.path.join(TMP, "two-colours.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1e3cdc:s=640x360:r=24:d=4",
                        "-f", "lavfi", "-i", "color=c=0x1ea03c:s=640x360:r=24:d=4", "-filter_complex",
                        "[0:v][1:v]concat=n=2:v=1:a=0", "-pix_fmt", "yuv420p", rec], check=True)
        tall = os.path.join(TMP, "tall.mp4")
        open(tall, "wb").write(video("tall-src.mp4", (180, 390), 6, "color=c=0x1e3cdc"))
        shot = os.path.join(TMP, "wide.png")
        Image.new("RGB", (800, 500), (240, 200, 30)).save(shot)
        long_shot = os.path.join(TMP, "long.png")
        Image.new("RGB", (400, 900), (240, 200, 30)).save(long_shot)
        BLUE, GREEN, RED, YELLOW, DARK = (30, 60, 220), (30, 160, 60), (200, 30, 30), (240, 200, 30), (17, 17, 17)
        x, y, w, h, band = stitch.split_box(size, (640, 360))
        check((x, y, w, h, band) == (6, 22, 168, 94, 122) and stitch.split_box((720, 1280), (1920, 1080)) == (22, 90, 676, 380, 492)
              and stitch.split_box((720, 1280), (1000, 1000))[4] == 640 and stitch.split_box((720, 1280), (1000, 1000))[2] == 528,
              "the screen is as wide as the frame inside a small margin, under what a platform covers; a squarer one "
              "is shown narrower so its band never takes more than half the frame")
        v = {"video": rec, "format": "mov", "start": 0, "speed": 1.0, "layout": "split"}
        check(stitch.screen_layout(v, size) == "split" and stitch.screen_layout(dict(v, layout="pip"), size) == "pip"
              and stitch.screen_layout({"video": rec, "format": "mov"}, size) == "pip"
              and stitch.screen_layout(dict(v, video=tall), size) == "pip"
              and stitch.screen_layout(v, (320, 180)) == "pip"
              and stitch.screen_layout({"images": [shot], "layout": "split"}, size) == "split"
              and stitch.screen_layout({"images": [shot, long_shot], "layout": "split"}, size) == "pip"
              and stitch.screen_layout({"images": [os.path.join(TMP, "none.png")], "layout": "split"}, size) == "pip"
              and stitch.screen_layout(None, size) == "pip" and stitch.screen_layout(dict(v, size=(100, 300)), size) == "pip"
              and stitch.video_size(rec, "mov") == (640, 360) and stitch.video_size(shot, "mov") is None,
              "split only when the recipe asks, the frame is upright and everything shown is wide; a tall phone "
              "screen, a mixed set or a wide frame keeps the corner layout")
        out = os.path.join(TMP, "split.mp4")
        info = stitch.render_beat(presenter, out, size=size, fps=fps, seconds=6, screen=v)
        mid, low = (x + w // 2, y + h // 2), (90, band + (320 - band) // 2)
        check(abs(info["duration"] - 6.0) < 0.1 and info["audio"] and (info["width"], info["height"]) == size
              and near(frame_px(out, 1.0, mid), BLUE) and near(frame_px(out, 5.0, mid), GREEN)
              and near(frame_px(out, 1.0, low), RED) and near(frame_px(out, 5.0, low), RED)
              and near(frame_px(out, 3.0, (90, 8)), DARK, 30) and near(frame_px(out, 3.0, (2, band + 4)), RED),
              "split: the recording plays across the top as it is (blue, then green after four seconds), the presenter "
              "fills the frame below it edge to edge, and the beat keeps its sound")
        out = os.path.join(TMP, "split-fast.mp4")
        stitch.render_beat(presenter, out, size=size, fps=fps, seconds=6, screen=dict(v, speed=2.0))
        check(near(frame_px(out, 1.0, mid), BLUE) and near(frame_px(out, 2.6, mid), GREEN) and near(frame_px(out, 5.5, mid), GREEN),
              "at twice the speed its second half arrives after two seconds, and its last picture is held when it runs out")
        out = os.path.join(TMP, "split-later.mp4")
        stitch.render_beat(presenter, out, size=size, fps=fps, seconds=6, screen=dict(v, start=4.5))
        check(near(frame_px(out, 0.3, mid), GREEN) and near(frame_px(out, 5.5, mid), GREEN),
              "a later beat starts where the one before it stopped")
        out = os.path.join(TMP, "split-shot.mp4")
        stitch.render_beat(presenter, out, size=size, fps=fps, seconds=6, screen={"images": [shot], "layout": "split"})
        check(near(frame_px(out, 3.0, mid), YELLOW) and near(frame_px(out, 3.0, low), RED),
              "a wide screenshot gets the same layout")
        out = os.path.join(TMP, "pip-tall.mp4")
        stitch.render_beat(presenter, out, size=size, fps=fps, seconds=6, screen=dict(v, video=tall))
        px, py, pw, ph = stitch.pip_box(size)
        check(near(frame_px(out, 3.0, (90, 60)), BLUE) and near(frame_px(out, 3.0, (px + pw // 2, py + ph // 2)), RED, 60),
              "a tall phone recording fills the frame with the presenter small in the corner")
        try:
            stitch.render_beat(presenter, os.path.join(TMP, "x.mp4"), size=size, fps=fps, seconds=6,
                               screen={"video": os.path.join(TMP, "gone.mp4"), "format": "mov", "layout": "split"})
            check(False, "a missing recording must raise")
        except stitch.StitchError as e:
            check("recording isn't there" in str(e), "a recording that has gone stops the render with a clear reason")
    else:
        print("  SKIPPED: reading and rendering video needs ffmpeg and ffprobe")
    print(f"\n{passed} recordings checks passed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

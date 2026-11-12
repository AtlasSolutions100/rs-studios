"""A product's screen recording (m38): the member's own capture of their app or website.

A video preset that shows the member's screens (presets.screens_needed == "app_screens": Show your app, Web app
demo) plays it in its "screen" beats, in place of screenshots. It is shown as it is: scaled to the frame, sped up at
most MAX_SPEED times so more of it fits, and never sent to a model. Its sound is not used. One per product; a new
upload replaces the last.

What is checked before it is kept (inspect):
  * the file's own first bytes say MP4/MOV or WebM (the browser's label can say anything), and ffprobe and ffmpeg
    are then told that container outright (`-f`), so a playlist or a script dressed as a video is never opened;
  * it has a picture, MIN_SECONDS to MAX_SECONDS long, no side over MAX_SIDE pixels.

The file lives in the workspace's uploads folder, so it counts toward its storage (storage.used_bytes) and goes
when the workspace is deleted (wsdelete.py). Rows: `recordings` (db.py).
"""
import json
import logging
import os
import shutil
import subprocess

from flask import Blueprint, abort, flash, g, redirect, request, send_file, url_for

import db
import products
from core import MAX_VIDEO_MB, UPLOADS, VIDEO_TYPES, login_required, save_upload

log = logging.getLogger("campaignify")
bp = Blueprint("recordings", __name__)

MIN_SECONDS, MAX_SECONDS = 3, 180
MAX_SIDE = 4096
MAX_SPEED = 2.0             # a recording longer than its beats plays up to this fast; past that, its start is shown
_MOV_ATOMS = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")
UNREADABLE = "That file isn't a video we can read. Export the recording as an MP4 or a MOV and add that."


def container(path: str) -> str:
    """The ffmpeg demuxer for the file, read from its first bytes: "mov" (MP4, MOV, M4V), "matroska" (WebM) or ""."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return ""
    if head[4:8] in _MOV_ATOMS:
        return "mov"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    return ""


def inspect(path: str) -> dict:
    """{"format", "seconds", "width", "height"} of a recording that can be used. Raises ValueError (said to the member)."""
    fmt = container(path)
    if not fmt:
        raise ValueError(UNREADABLE)
    if not shutil.which("ffprobe"):
        raise ValueError("Screen recordings can't be read on this server right now. Add screenshots instead.")
    import stitch
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-f", fmt, "-select_streams", "v:0", "-show_entries",
                            "stream=duration:format=duration", "-of", "json", path], capture_output=True, timeout=60)
        d = json.loads(r.stdout or b"{}")
    except (subprocess.TimeoutExpired, ValueError, OSError):
        raise ValueError(UNREADABLE) from None

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    w, h = stitch.video_size(path, fmt) or (0, 0)        # as it is shown: a clip marked as turned counts turned
    secs = num((d.get("format") or {}).get("duration")) or num(((d.get("streams") or [{}])[0]).get("duration"))
    if r.returncode != 0 or not w or not h or secs <= 0:
        raise ValueError(UNREADABLE)
    if secs < MIN_SECONDS:
        raise ValueError(f"That recording is under {MIN_SECONDS} seconds. Record a little more of the screen.")
    if secs > MAX_SECONDS:
        raise ValueError(f"Recordings up to {MAX_SECONDS // 60} minutes. Trim it to the part you want shown; a video "
                         "shows about its first 15 seconds.")
    if max(w, h) > MAX_SIDE:
        raise ValueError(f"That recording is {w} x {h} pixels. Export it at 4K or smaller.")
    return {"format": fmt, "seconds": round(secs, 2), "width": w, "height": h}


def of_product(conn, ws_id, product_id):
    """The product's recording (a row), or None. One whose file has gone counts as none."""
    if not product_id:
        return None
    r = conn.execute("SELECT * FROM recordings WHERE workspace_id=? AND product_id=? ORDER BY id DESC LIMIT 1",
                     (ws_id, product_id)).fetchone()
    return r if r is not None and r["path"] and os.path.exists(r["path"]) else None


def get(conn, ws_id, rid):
    r = conn.execute("SELECT * FROM recordings WHERE id=? AND workspace_id=?", (rid or 0, ws_id)).fetchone()
    return r if r is not None and r["path"] and os.path.exists(r["path"]) else None


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def remove(conn, ws_id, product_id) -> int:
    """Take the product's recording away, file and row. How many rows went."""
    rows = conn.execute("SELECT * FROM recordings WHERE workspace_id=? AND product_id=?", (ws_id, product_id)).fetchall()
    for r in rows:
        _unlink(r["path"])
    conn.execute("DELETE FROM recordings WHERE workspace_id=? AND product_id=?", (ws_id, product_id))
    conn.commit()
    return len(rows)


def add(conn, ws, product_id, fs) -> dict:
    """Keep an uploaded recording as the product's (replacing the last). Returns inspect()'s dict. Raises
    ValueError with what to tell the member, or storage.StorageFull."""
    path, ctype = save_upload(fs, os.path.join(UPLOADS, str(ws["id"])), VIDEO_TYPES, MAX_VIDEO_MB, ws=ws)
    try:
        info = inspect(path)
    except ValueError:
        _unlink(path)
        raise
    remove(conn, ws["id"], product_id)
    conn.execute("INSERT INTO recordings (workspace_id, product_id, path, filename, content_type, seconds, width, height, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (ws["id"], product_id, path, (fs.filename or "")[:120], ctype, info["seconds"], info["width"],
                  info["height"], db.now()))
    conn.commit()
    return info


def window(seconds: float, need: float) -> tuple:
    """(speed, source seconds shown) for a recording `seconds` long played over `need` seconds of video: as it is
    when it fits, faster (never past MAX_SPEED) when it is longer, and its start alone when it is longer still."""
    need = max(0.1, float(need or 0))
    speed = min(MAX_SPEED, max(1.0, float(seconds or 0) / need))
    return round(speed, 3), round(min(float(seconds or 0), need * speed), 3)


def summary(row) -> str:
    """"0:42, 1920 x 1080" for the product page."""
    s = int(round(row["seconds"] or 0))
    return f"{s // 60}:{s % 60:02d}, {row['width']} x {row['height']}"


# ---------------------------------------------------------------- pages

@bp.route("/app/products/<int:pid>/recording", methods=["POST"])
@login_required
def save(pid):
    """Add, replace or remove the product's screen recording (the product page's form)."""
    back = url_for("products.edit", pid=pid) + "#recording"
    with db.connect() as conn:
        if products.get_product(conn, g.ws["id"], pid) is None:
            abort(404)
        if request.form.get("do") == "remove":
            if remove(conn, g.ws["id"], pid):
                flash("Removed the screen recording.", "ok")
            return redirect(back)
        fs = request.files.get("recording")
        if not fs or not fs.filename:
            flash("Pick a screen recording to add.")
            return redirect(back)
        try:
            info = add(conn, g.ws, pid, fs)
        except ValueError as e:
            flash(str(e))
            return redirect(back)
    flash(f"Added your screen recording ({int(round(info['seconds']))} seconds). Video presets that show your screens "
          "will play it.", "ok")
    return redirect(back)


@bp.route("/app/recordings/<int:rid>")
@login_required
def view(rid):
    """The recording itself, for its owner's page."""
    with db.connect() as conn:
        r = get(conn, g.ws["id"], rid)
    if r is None:
        abort(404)
    return send_file(r["path"], mimetype=r["content_type"] or "video/mp4", conditional=True)

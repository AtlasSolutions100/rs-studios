"""Background jobs: submit pending generations, poll running ones, save the
results to disk, refund credits for anything that failed.

`tick()` does one pass and is what the tests drive; `start()` runs it in a
daemon thread inside the web process (one process is plenty at this scale).
"""
import json
import mimetypes
import os
import threading
import time
import traceback

import requests

import catalog
import db
import hf

MEDIA = os.path.join(db.DATA, "media")
POLL_SECONDS = float(os.environ.get("RS_POLL_SECONDS", "5"))
MAX_ATTEMPTS = 3
STUCK_MINUTES = 30


def _set(conn, jid, **kw):
    kw["updated_at"] = db.now()
    conn.execute(f"UPDATE jobs SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), jid))
    conn.commit()


def _remote_url(conn, table: str, row) -> str:
    """Upload a stored photo or reference video once; cache its public URL."""
    if row["remote_url"]:
        return row["remote_url"]
    with open(row["path"], "rb") as f:
        data = f.read()
    ctype = row["content_type"] or mimetypes.guess_type(row["path"])[0] or "application/octet-stream"
    url = hf.upload(data, ctype)
    conn.execute(f"UPDATE {table} SET remote_url=? WHERE id=?", (url, row["id"]))
    conn.commit()
    return url


def build_inputs(conn, job) -> dict:
    p = json.loads(job["params_json"] or "{}")
    tool = job["tool"]
    photos = [conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, job["workspace_id"])).fetchone()
              for pid in p.get("photo_ids", [])]
    photos = [ph for ph in photos if ph]
    urls = [_remote_url(conn, "photos", ph) for ph in photos]
    if tool == "ad_image":
        return {"prompt": job["brief"], "image_urls": urls[:10], "niche": p.get("niche") or "",
                "language": "English", "batch_size": int(p.get("batch") or 1),
                "aspect_ratio": p.get("aspect") or "9:16", "target_audience": p.get("audience") or ""}
    if tool == "photo_to_video":
        if not urls:
            raise hf.GenError("Pick a product photo first.")
        return {"prompt": job["brief"], "image_url": urls[0], "duration": int(p.get("duration") or 5),
                "resolution": "720p", "bitrate_mode": "high", "generate_audio": bool(p.get("audio"))}
    if tool == "motion_copy":
        ref = conn.execute("SELECT * FROM refs WHERE id=?", (p.get("ref_id"),)).fetchone()
        if not ref:
            raise hf.GenError("Pick a reference video first.")
        return {"prompt": job["brief"] or "", "video_url": _remote_url(conn, "refs", ref),
                "image_urls": urls[:8], "resolution": "720p"}
    raise hf.GenError(f"unknown tool {tool}")


def _refund(conn, job, reason):
    if job["credits"]:
        db.adjust_credits(conn, job["workspace_id"], job["credits"], f"refund: {reason}", job["id"])


def _fail(conn, job, msg):
    _refund(conn, job, msg[:60])
    _set(conn, job["id"], status="failed", error=msg[:500])


def _save_outputs(conn, job, result):
    outs = hf.outputs(result)
    if not outs:
        raise hf.GenError("finished with no files")
    folder = os.path.join(MEDIA, str(job["workspace_id"]), str(job["id"]))
    os.makedirs(folder, exist_ok=True)
    for i, o in enumerate(outs, start=1):
        ext = ".mp4" if o["kind"] == "video" else ".png"
        if "." in o["url"].rsplit("/", 1)[-1].split("?")[0]:
            ext = "." + o["url"].rsplit("/", 1)[-1].split("?")[0].rsplit(".", 1)[-1][:5]
        path = os.path.join(folder, f"rs-studios-{job['id']}-{i}{ext}")
        try:
            r = requests.get(o["url"], timeout=300)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        except Exception:  # noqa: BLE001 — keep the remote URL if the copy fails
            path = None
        if path and o["kind"] == "video":
            make_poster(path)
        conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                     (job["id"], o["kind"], o["url"], path))
    conn.commit()


def make_poster(video_path: str) -> str | None:
    """A still for the library grid (<video poster>): <file>.jpg next to the video."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return None
    out = video_path + ".jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", video_path, "-frames:v", "1",
                    "-vf", "scale=540:-2", "-q:v", "4", out], capture_output=True, timeout=60)
    return out if os.path.exists(out) else None


def tick() -> int:
    """One pass over pending and running jobs. Returns how many changed."""
    changed = 0
    with db.connect() as conn:
        for job in conn.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 5").fetchall():
            try:
                inputs = build_inputs(conn, job)
                rid = hf.submit(catalog.TOOLS[job["tool"]]["model"], inputs)
                _set(conn, job["id"], status="running", request_id=rid, attempts=job["attempts"] + 1)
            except hf.GenError as e:
                _fail(conn, job, str(e))
            except Exception as e:  # noqa: BLE001
                if job["attempts"] + 1 >= MAX_ATTEMPTS:
                    _fail(conn, job, f"could not start: {e}")
                else:
                    _set(conn, job["id"], attempts=job["attempts"] + 1, error=str(e)[:300])
            changed += 1
        for job in conn.execute("SELECT * FROM jobs WHERE status='running' ORDER BY id LIMIT 20").fetchall():
            try:
                res = hf.status(job["request_id"])
            except Exception:  # noqa: BLE001 — try again next tick
                continue
            st = (res.get("status") or "").lower()
            if st == "completed":
                try:
                    _save_outputs(conn, job, res)
                    _set(conn, job["id"], status="done", error=None)
                except Exception as e:  # noqa: BLE001
                    _fail(conn, job, f"result unreadable: {e}")
                changed += 1
            elif st == "nsfw":
                _fail(conn, job, "Blocked by the content filter. Try a different brief or photo.")
                changed += 1
            elif st in hf.FINAL:
                _fail(conn, job, "The generation failed. Your credits were refunded.")
                changed += 1
            elif job["updated_at"] < _minutes_ago(STUCK_MINUTES * 4):
                _fail(conn, job, "Timed out. Your credits were refunded.")
                changed += 1
    return changed


def _minutes_ago(m):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=m)).replace(microsecond=0).isoformat()


_thread = None


def start():
    global _thread
    if _thread or os.environ.get("RS_WORKER", "1") != "1":
        return

    def loop():
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(POLL_SECONDS)

    _thread = threading.Thread(target=loop, name="rs-worker", daemon=True)
    _thread.start()

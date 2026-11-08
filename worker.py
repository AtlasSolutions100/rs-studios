"""Background jobs: submit pending generations, poll running ones, save the
results to disk, refund credits for anything that failed.

`tick()` does one pass and is what the tests drive; `start()` runs it in a
daemon thread inside the web process (one process is plenty at this scale).
"""
import json
import mimetypes
import os
import sqlite3
import threading
import time
import traceback

import requests

import logging

import catalog
import db
import hf

log = logging.getLogger("campaignify")
# A talking video is refused (and refunded) if the vendor's estimate says it now
# charges more than this times what we priced it at; the admin page shows the live
# estimates. With no readable estimate (vendor down, reply format changed) the video
# goes ahead at our own table price: that's the price the member was quoted and
# paid, and the margin (MARKUP) covers drift. The miss is logged and shown on admin.
PRICE_TOLERANCE = 1.25
ESTIMATE_META = "vendor_estimate_live"

MEDIA = os.path.join(db.DATA, "media")
POLL_SECONDS = float(os.environ.get("RS_POLL_SECONDS", "5"))
MAX_ATTEMPTS = 3
STUCK_MINUTES = 30
# A submit in flight holds a claim on its job (jobs.claimed_at) so no second pass or
# process sends it too. A claim older than this belongs to a worker that died mid-call:
# the job is picked up again and resent with the same idempotency key (jobs.submit_key),
# which the vendor answers with the first request id instead of a second, paid one.
SUBMIT_LEASE_MINUTES = 10
# A job whose submit failed for a passing reason (vendor busy, 5xx, network) waits
# this long times 2^(tries-1) before its next try: 30 s, then 60 s.
RETRY_BASE_SECONDS = float(os.environ.get("RS_JOB_RETRY_SECONDS", "30"))
CANCELED = "You canceled it."


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


def image_prompt(conn, job, p: dict | None = None) -> str:
    """The image model's prompt for an image ad job: the scene from the brief
    plus a strict list of the only words allowed on the image (the member's
    headline, optional subline and button, the brand name). The product's
    claims, approved or only suggested, never go in unless the member typed
    that text as the headline, subline or button."""
    p = p if p is not None else json.loads(job["params_json"] or "{}")
    parts = catalog.split_image_brief(job["brief"] or "")
    keys = job.keys() if hasattr(job, "keys") else []
    prod = conn.execute("SELECT * FROM products WHERE id=? AND workspace_id=?",
                        (job["product_id"], job["workspace_id"])).fetchone() \
        if "product_id" in keys and job["product_id"] else None
    brand = conn.execute("SELECT brand_name FROM brands WHERE id=? AND workspace_id=?",
                         (job["brand_id"], job["workspace_id"])).fetchone() \
        if "brand_id" in keys and job["brand_id"] else None
    forbid = []
    if prod is not None:
        forbid += [c for c in (prod["claims"] or "").splitlines() if c.strip()]
        try:
            forbid += [c for c in json.loads(prod["claims_pending"] or "[]") if isinstance(c, str)]
        except (ValueError, TypeError):
            pass
    return catalog.image_ad_prompt(parts["scene"], p.get("headline") or parts["headline"],
                                   p.get("subline") or parts["subline"], p.get("cta") or parts["cta"],
                                   brand=(brand["brand_name"] if brand else "") or "",
                                   product=p.get("product") or (prod["name"] if prod is not None else ""), forbid=forbid)


def build_inputs(conn, job) -> dict:
    p = json.loads(job["params_json"] or "{}")
    tool = job["tool"]
    photos = [conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, job["workspace_id"])).fetchone()
              for pid in p.get("photo_ids", [])]
    photos = [ph for ph in photos if ph]
    urls = [_remote_url(conn, "photos", ph) for ph in photos]
    if tool == "ad_image":
        return {"prompt": image_prompt(conn, job, p), "image_urls": urls[:10], "niche": p.get("niche") or "",
                "language": "English", "batch_size": int(p.get("batch") or 1),
                "aspect_ratio": p.get("aspect") or "9:16", "target_audience": p.get("audience") or ""}
    if tool == "photo_to_video":
        if not urls:
            raise hf.GenError("Pick a product photo first.")
        return {"prompt": job["brief"], "image_url": urls[0], "duration": int(p.get("duration") or 5),
                "resolution": p.get("resolution") or "720p", "bitrate_mode": "high", "generate_audio": bool(p.get("audio"))}
    if tool == "ugc_video":
        if not urls:
            raise hf.GenError("Pick a product photo first.")
        prod = conn.execute("SELECT name FROM products WHERE id=?", (job["product_id"],)).fetchone() \
            if "product_id" in job.keys() and job["product_id"] else None
        prompt = catalog.talking_prompt(p.get("ad_type") or "ugc", p.get("product") or (prod["name"] if prod else ""),
                                        job["brief"] or "", p.get("presenter") or "", p.get("voice") or "",
                                        p.get("setting") or "")
        return {"prompt": prompt, "image_urls": urls[:4], "duration": int(p.get("duration") or 10),
                "aspect_ratio": p.get("aspect") or "9:16", "resolution": p.get("resolution") or "720p",
                "bitrate_mode": "high",
                "generate_audio": True}
    if tool == "video_upscale":
        src = conn.execute("SELECT o.url FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.job_id=? AND j.workspace_id=? "
                           "AND o.kind='video' ORDER BY o.id LIMIT 1", (p.get("source_job"), job["workspace_id"])).fetchone()
        if not src or not src["url"]:
            raise hf.GenError("The original video isn't available to upscale.")
        return {"video_url": src["url"]}
    if tool == "motion_copy":
        ref = conn.execute("SELECT * FROM refs WHERE id=?", (p.get("ref_id"),)).fetchone()
        if not ref:
            raise hf.GenError("Pick a reference video first.")
        return {"prompt": job["brief"] or "", "video_url": _remote_url(conn, "refs", ref),
                "image_urls": urls[:8], "resolution": "720p"}
    raise hf.GenError(f"unknown tool {tool}")


def _refund(conn, job, reason):
    db.refund_job(conn, job, f"refund: {reason}")


REFUNDED = "Credits refunded."


def failure_text(msg: str) -> str:
    """Every failure the member sees ends with the same plain promise."""
    msg = " ".join(str(msg or "").split()).rstrip()
    for old in ("Your credits were refunded.", "Your credits were refunded", "Credits refunded."):
        if msg.endswith(old):
            msg = msg[: -len(old)].rstrip()
    if msg and msg[-1] not in ".!?":
        msg += "."
    return (msg + " " + REFUNDED).strip()


def _fail(conn, job, msg, extra_where: str = "", extra_args: tuple = ()) -> bool:
    """Mark the job failed and refund it, in one transaction. Only the first to move
    the job out of the status it was read with refunds (a member's cancel and the
    worker can't both refund one job). False when someone else got there first."""
    t = db.now()
    cur = conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=?, claimed_at=NULL "
                       f"WHERE id=? AND status=?{extra_where}",
                       (failure_text(msg)[:500], t, t, job["id"], job["status"], *extra_args))
    if cur.rowcount != 1:
        conn.rollback()
        return False
    _refund(conn, job, msg[:60])
    if _is_preview(job):
        import preview
        preview.release(conn, job["id"])
    conn.commit()
    return True


def _is_preview(job) -> bool:
    try:
        return bool(json.loads(job["params_json"] or "{}").get("preview"))
    except (ValueError, TypeError):
        return False


WATERMARK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "img", "preview-watermark.png")


def watermark(path: str) -> bool:
    """Burn the PREVIEW mark into a free preview video, in place. False if it couldn't."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg") or not os.path.exists(WATERMARK):
        return False
    tmp = path + ".wm.mp4"
    vf = ("[1:v][0:v]scale2ref=w=main_w*0.7:h=ow/mdar[wm][base];"
          "[base][wm]overlay=x=(main_w-overlay_w)/2:y=main_h*0.66:format=auto,format=yuv420p[v]")
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-i", WATERMARK, "-filter_complex", vf,
                            "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                            "-c:a", "copy", "-movflags", "+faststart", tmp], capture_output=True, timeout=180)
    except Exception:  # noqa: BLE001
        r = None
    if not r or r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
        if os.path.exists(tmp):
            os.remove(tmp)
        log.warning("watermark failed for %s: %s", path, (r.stderr[-300:] if r else b"").decode(errors="ignore"))
        return False
    os.replace(tmp, path)
    return True


def _save_outputs(conn, job, result):
    outs = hf.outputs(result)
    if not outs:
        raise hf.GenError("finished with no files")
    if _is_preview(job):
        return _save_preview(conn, job, outs)
    folder = os.path.join(MEDIA, str(job["workspace_id"]), str(job["id"]))
    os.makedirs(folder, exist_ok=True)
    import storage
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (job["workspace_id"],)).fetchone()
    used = storage.used_bytes(job["workspace_id"], conn)
    for i, o in enumerate(outs, start=1):
        ext = ".mp4" if o["kind"] == "video" else ".png"
        if "." in o["url"].rsplit("/", 1)[-1].split("?")[0]:
            ext = "." + o["url"].rsplit("/", 1)[-1].split("?")[0].rsplit(".", 1)[-1][:5]
        path = os.path.join(folder, f"campaignify-{job['id']}-{i}{ext}")
        try:
            storage.guard(ws, 0, used)      # disk under 5 GB or quota already used: keep only the vendor's link
            r = requests.get(o["url"], timeout=300)
            r.raise_for_status()
            storage.guard(ws, len(r.content), used)
            with open(path, "wb") as f:
                f.write(r.content)
            used += len(r.content)
        except storage.StorageFull as e:
            log.warning("job %s output %s kept as a link only: %s", job["id"], i, type(e).__name__)
            path = None
        except Exception:  # noqa: BLE001 — keep the remote URL if the copy fails
            path = None
        if path and o["kind"] == "video":
            make_poster(path)
        conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                     (job["id"], o["kind"], o["url"], path))
    conn.commit()


def _save_preview(conn, job, outs):
    """A free preview is only ever kept watermarked: the vendor's own copy is
    never linked, and if the mark can't be added the preview fails (and can be
    tried again)."""
    o = next((x for x in outs if x["kind"] == "video"), None)
    if not o:
        raise hf.GenError("finished with no video")
    import storage
    try:
        storage.disk_guard()
    except storage.DiskFull as e:
        raise hf.GenError("The preview couldn't be saved: our server is short on space. Try again later.") from e
    folder = os.path.join(MEDIA, str(job["workspace_id"]), str(job["id"]))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"campaignify-{job['id']}-preview.mp4")
    try:
        r = requests.get(o["url"], timeout=300)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    except Exception as e:  # noqa: BLE001
        raise hf.GenError("The preview couldn't be saved. Try again.") from e
    if not watermark(path):
        os.remove(path)
        raise hf.GenError("The preview couldn't be finished. Try again.")
    make_poster(path)
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (job["id"], "video", "", path))
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


def table_cost(job) -> float:
    """What we priced this job at: its stored cost, else today's table price."""
    if job["cost_usd"]:
        return float(job["cost_usd"])
    p = json.loads(job["params_json"] or "{}")
    try:
        return catalog.cost_usd(job["tool"], duration=float(p.get("duration") or 10),
                                resolution=p.get("resolution") or catalog.DEFAULT_RESOLUTION)
    except (KeyError, TypeError, ValueError):
        return 0.0


def note_estimate(conn, **kw):
    """The last live estimate the worker asked for (admin health card + warning)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (ESTIMATE_META,)).fetchone()
        st = json.loads(row["value"]) if row else {}
    except (ValueError, TypeError):
        st = {}
    if kw.get("ok"):
        st.update(misses=0, last_ok_at=kw["at"])
    else:
        st["misses"] = int(st.get("misses") or 0) + 1
    st.update(kw)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (ESTIMATE_META, json.dumps(st)))
    conn.commit()


def price_ok(conn, job, model: str, inputs: dict) -> bool:
    """May this talking video start? False only when the vendor's readable estimate is
    over PRICE_TOLERANCE x our price. A missing or unreadable estimate fails open."""
    ours = table_cost(job)
    vendor = hf.estimate(model, inputs)
    why = None if vendor is not None else (hf.last_estimate.get("why") or "no estimate")
    try:
        note_estimate(conn, at=db.now(), ok=vendor is not None, job=job["id"], vendor=vendor, ours=round(ours, 4),
                      why=why and str(why)[:300])
    except Exception:  # noqa: BLE001 — bookkeeping never blocks a video
        log.exception("couldn't record the vendor estimate")
    if vendor is None:
        log.warning("job %s: no vendor estimate (%s); starting at our table price $%.3f", job["id"], why, ours)
        return True
    if ours > 0 and vendor > ours * PRICE_TOLERANCE:
        log.warning("job %s: vendor estimate $%.3f over our price $%.3f; refused", job["id"], vendor, ours)
        return False
    return True


def _beat():
    """Heartbeat for /health, only from the worker thread (tests drive tick() directly)."""
    if _thread is not None and threading.current_thread() is _thread:
        import alerts
        alerts.beat()


def _busy(what: str):
    """Keep the heartbeat going while one long call runs (alerts.busy; a no-op off the worker thread)."""
    import alerts
    return alerts.busy(what)


def _lease_cutoff() -> str:
    return _minutes_ago(SUBMIT_LEASE_MINUTES)


def _due(job) -> bool:
    """A job that already had a try waits out its backoff before the next."""
    if not job["attempts"]:
        return True
    wait = RETRY_BASE_SECONDS * 2 ** (job["attempts"] - 1)
    return job["updated_at"] <= _minutes_ago(wait / 60)


def _claim(conn, job) -> sqlite3.Row | None:
    """Take a pending job for one submit try, writing its idempotency key first
    (kept across tries). None if another pass holds a live claim on it."""
    t = db.now()
    cur = conn.execute("UPDATE jobs SET claimed_at=?, submit_key=COALESCE(submit_key, ?), attempts=attempts+1, "
                       "updated_at=? WHERE id=? AND status='pending' AND (claimed_at IS NULL OR claimed_at < ?)",
                       (t, hf.new_key(), t, job["id"], _lease_cutoff()))
    conn.commit()
    if cur.rowcount != 1:
        return None
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()


def _try_later(conn, job, msg: str) -> bool:
    """A passing failure: back in the queue with its key, or failed (and refunded)
    after MAX_ATTEMPTS tries."""
    if job["attempts"] >= MAX_ATTEMPTS:
        return _fail(conn, job, msg)
    cur = conn.execute("UPDATE jobs SET claimed_at=NULL, error=?, updated_at=? WHERE id=? AND status='pending'",
                       (msg[:300], db.now(), job["id"]))
    conn.commit()
    return cur.rowcount == 1


def _submit_one(conn, job):
    job = _claim(conn, job)
    if job is None:
        return False
    try:
        inputs = build_inputs(conn, job)
        model = catalog.TOOLS[job["tool"]]["model"]
        if job["tool"] == "ugc_video" and not price_ok(conn, job, model, inputs):
            raise hf.GenError("This format's price changed and it couldn't start. We've been told.")
        # uploads and the price check can take a while: renew the claim right before the call
        _set(conn, job["id"], claimed_at=db.now())
        rid = hf.submit(model, inputs, idempotency_key=job["submit_key"])
        _set(conn, job["id"], status="running", request_id=rid, claimed_at=None, error=None)
    except hf.Transient as e:
        if hf.retries_on():
            _try_later(conn, job, str(e))
        else:
            _fail(conn, job, str(e))
    except hf.GenError as e:
        _fail(conn, job, str(e))
    except Exception as e:  # noqa: BLE001
        _try_later(conn, job, f"Couldn't start: {e}")
    return True


def _timed_out(conn, job) -> bool:
    """Past the deadline: ask the vendor to drop the request first (BILL-5), so a
    job we refund is never also billed to us later. The vendor only drops one that
    hasn't started; one already being made can't be stopped, so it's logged with
    its request id to match against the vendor's bill. The member is refunded
    either way."""
    outcome = "no request"
    if job["request_id"]:
        outcome = hf.cancel(job["request_id"])
    if outcome != hf.CANCELED:
        log.warning("job %s timed out; vendor cancel: %s (request %s)", job["id"], outcome, job["request_id"])
    return _fail(conn, job, "Timed out.")


def cancel_job(conn, job) -> tuple[bool, str]:
    """A member cancels a queued ad. A job we haven't sent is dropped here; one
    the vendor has but hasn't started is canceled there. Either way the credits
    come back. One the vendor is already making can't be stopped."""
    if job["status"] == "pending":
        if job["claimed_at"] and job["claimed_at"] >= _lease_cutoff():
            return False, "It's starting right now. Try again in a moment."
        if _fail(conn, job, CANCELED, " AND (claimed_at IS NULL OR claimed_at < ?)", (_lease_cutoff(),)):
            return True, failure_text(CANCELED)
        return False, "It's starting right now. Try again in a moment."
    if job["status"] != "running":
        return False, "It has already finished."
    outcome = hf.cancel(job["request_id"]) if job["request_id"] else hf.GONE
    if outcome in (hf.CANCELED, hf.GONE):
        if _fail(conn, job, CANCELED):
            return True, failure_text(CANCELED)
        return False, "It has already finished."
    if outcome == hf.STARTED:
        return False, "It's already being made and can't be stopped now."
    log.warning("job %s: member cancel failed at the vendor: %s", job["id"], outcome)
    return False, "It couldn't be canceled right now. Try again in a moment."


def tick() -> int:
    """One pass over pending and running jobs. Returns how many changed."""
    changed = 0
    with db.connect() as conn:
        tried = 0
        for job in conn.execute("SELECT * FROM jobs WHERE status='pending' AND (claimed_at IS NULL OR claimed_at < ?) "
                                "ORDER BY priority DESC, id LIMIT 100", (_lease_cutoff(),)).fetchall():
            if tried >= 5:
                break
            _beat()
            if conn.execute("SELECT 1 FROM workspaces WHERE id=? AND COALESCE(billing_hold, 0)=1",
                            (job["workspace_id"],)).fetchone():
                # A refund or dispute paused the workspace (billing_holds.py): never start it.
                _fail(conn, job, "Paused: a payment on this workspace was reversed.")
                changed += 1
                continue
            if not _due(job):       # waiting out a backoff: let the jobs behind it go first
                continue
            tried += 1
            with _busy("vendor submit"):   # upload + submit with retries: minutes when the vendor is slow (N2)
                if _submit_one(conn, job):
                    changed += 1
        for job in conn.execute("SELECT * FROM jobs WHERE status='running' ORDER BY id LIMIT 20").fetchall():
            _beat()
            try:
                with _busy("vendor status"):
                    res = hf.status(job["request_id"])
            except Exception:  # noqa: BLE001 — try again next tick, but not forever
                # A request the vendor no longer knows (404) or a status call that
                # keeps failing must still end: same deadline as a job that never finishes.
                if job["updated_at"] < _minutes_ago(STUCK_MINUTES * 4):
                    _timed_out(conn, job)
                    changed += 1
                continue
            st = (res.get("status") or "").lower()
            if st == "completed":
                if not _still_running(conn, job):
                    continue    # a member's cancel got there first (refunded): don't deliver it (N11)
                try:
                    with _busy("saving outputs"):
                        _save_outputs(conn, job, res)
                    if not _finish_done(conn, job):
                        _drop_outputs(conn, job)
                except hf.GenError as e:
                    _fail(conn, job, str(e))
                except Exception as e:  # noqa: BLE001
                    _fail(conn, job, f"The result couldn't be read: {e}")
                changed += 1
            elif st == "nsfw":
                _fail(conn, job, "Blocked by the content filter. Try a different brief or photo.")
                changed += 1
            elif st in hf.FINAL:
                _fail(conn, job, "The generation failed.")
                changed += 1
            elif job["updated_at"] < _minutes_ago(STUCK_MINUTES * 4):
                _timed_out(conn, job)
                changed += 1
    return changed


def _still_running(conn, job) -> bool:
    r = conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
    return bool(r and r["status"] == "running")


def _finish_done(conn, job) -> bool:
    """Mark a finished job done, only if it is still running: a concurrent cancel
    (failed and refunded) is never overridden (N11). False when it lost."""
    t = db.now()
    cur = conn.execute("UPDATE jobs SET status='done', error=NULL, finished_at=?, updated_at=? WHERE id=? AND status='running'",
                       (t, t, job["id"]))
    conn.commit()
    return cur.rowcount == 1


def _drop_outputs(conn, job):
    """The job was canceled and refunded while its files were saved: remove them again."""
    import shutil
    log.warning("job %s finished after it was canceled; its outputs are not kept", job["id"])
    conn.execute("DELETE FROM outputs WHERE job_id=?", (job["id"],))
    conn.commit()
    shutil.rmtree(os.path.join(MEDIA, str(job["workspace_id"]), str(job["id"])), ignore_errors=True)


def _minutes_ago(m):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=m)).replace(microsecond=0).isoformat()


_thread = None


def start():
    global _thread
    if _thread or os.environ.get("RS_WORKER", "1") != "1":
        return

    def loop():
        import alerts
        while True:
            alerts.beat()
            try:
                tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            try:
                import flows_engine
                with alerts.busy("flows"):   # email and text sends
                    flows_engine.tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            try:
                import conversions   # retry server-side ad conversions that didn't go out
                with alerts.busy("conversions"):
                    conversions.tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            with alerts.busy("alert sweep"):   # includes the vendor probe
                alerts.maybe_sweep()   # operator alerts: stuck jobs, vendor errors, disk, backups (never raises)
            import storage
            storage.maybe_purge()  # once a day: temp leftovers and expired exports (never raises)
            import credits
            credits.maybe_release_stale()  # holds left by a crashed AI run go back after 30 min (never raises)
            import backup
            backup.maybe_run()     # nightly off-site backup in its own thread, once R2 is set (never raises)
            time.sleep(POLL_SECONDS)

    _thread = threading.Thread(target=loop, name="rs-worker", daemon=True)
    _thread.start()

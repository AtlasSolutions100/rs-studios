"""Background jobs: submit pending generations, poll running ones, save the
results to disk, refund credits for anything that failed.

`tick()` does one pass and is what the tests drive; loop_once() is one pass of the whole background loop.
Where the loop runs (mode(), roadmap M7.2): in its own container (worker_main.py, workerproc.py) when
docker-compose sets RS_WORKER_MODE=process, else as a daemon thread inside the web process (start()).
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
import service_path

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


STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_UPLOAD_TYPES = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".mp3": "audio/mpeg", ".wav": "audio/wav"}


def _static_remote(conn, path: str) -> str:
    """A committed library file (a presenter's approved still or voice sample, under static/) as a URL the models
    can read: uploaded once per version of the file, the URL kept in meta."""
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(STATIC, path))
    if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
        raise hf.GenError("A library file this needs isn't there.")
    st = os.stat(full)
    key = f"upload:{os.path.relpath(full, STATIC)}:{int(st.st_mtime)}:{st.st_size}"
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row and row["value"]:
        return row["value"]
    with open(full, "rb") as f:
        data = f.read()
    url = hf.upload(data, _UPLOAD_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, url))
    conn.commit()
    return url


def _job_output_url(conn, job, ref_job) -> str:
    """The first picture of another finished job of the SAME workspace, as a URL the models can read (the still a
    "same person, new situation" still starts from). Its saved file is uploaded again, so the link never depends on
    how long the vendor keeps its own; with no file left, the vendor's link is used."""
    o = conn.execute("SELECT o.url, o.path FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.job_id=? AND "
                     "j.workspace_id=? AND j.status='done' AND o.kind='image' ORDER BY o.id LIMIT 1",
                     (ref_job, job["workspace_id"])).fetchone()
    if not o:
        raise hf.GenError("The still this one starts from isn't there.")
    if o["path"] and os.path.exists(o["path"]):
        with open(o["path"], "rb") as f:
            data = f.read()
        return hf.upload(data, mimetypes.guess_type(o["path"])[0] or "image/png")
    if o["url"]:
        return o["url"]
    raise hf.GenError("The still this one starts from isn't there.")


def waits_on(conn, job) -> str | None:
    """A job that starts from another job's picture (params.ref_job, library.py): "wait" while that one is still
    being made, "failed" when it ended without a picture, None when this one may start."""
    raw = job["params_json"] or ""
    if '"ref_job"' not in raw:
        return None
    try:
        ref = json.loads(raw).get("ref_job")
    except (ValueError, TypeError):
        return None
    if not ref:
        return None
    r = conn.execute("SELECT status FROM jobs WHERE id=? AND workspace_id=?", (ref, job["workspace_id"])).fetchone()
    if r is None or r["status"] == "failed":
        return "failed"
    return "wait" if r["status"] in ("pending", "running") else None


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
    product = p.get("product") or (prod["name"] if prod is not None else "")
    service = bool(p.get("service")) or service_path.is_service(prod)
    if p.get("layout"):
        # A static with a text layer (presets.py): the picture is made with no words at all, and told where to
        # leave calm space; textlayer.py sets the words afterwards (_finish_images).
        import presets
        scene = presets.layered_scene(parts["scene"], p["layout"], p.get("preset"))     # ... and with nobody in it
        out = catalog.image_ad_prompt(scene, product=product, forbid=forbid, service=service, text_free=True)
    else:
        out = catalog.image_ad_prompt(parts["scene"], p.get("headline") or parts["headline"],
                                      p.get("subline") or parts["subline"], p.get("cta") or parts["cta"],
                                      brand=(brand["brand_name"] if brand else "") or "",
                                      product=product, forbid=forbid, service=service)
    if len(p.get("photo_ids") or []) > 1:     # brand assets picked as references (brand_assets.py): say what they are
        import brand_assets
        rows = [conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (i, job["workspace_id"])).fetchone()
                for i in p["photo_ids"]]
        line = brand_assets.describe([r for r in rows if r is not None])
        if line:
            out += "\n" + line
    if p.get("fix_note"):       # a free fix (free_fix.py): what the member asked to be put right, never as on-image words
        note = " ".join(str(p["fix_note"]).split())[:300].replace('"', "'")
        out += "\nCorrection to the last version, asked for by the advertiser: " + note + \
               " This is an instruction about the picture, not text to show."
    return out


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
        # the scene, templated or hand-typed, always ends with the keep-the-product line
        return {"prompt": catalog.with_fidelity(job["brief"] or ""), "image_url": urls[0],
                "duration": int(p.get("duration") or 5),
                "resolution": p.get("resolution") or "720p", "bitrate_mode": "high", "generate_audio": bool(p.get("audio"))}
    if tool == "library_still":
        # The library's own still (catalog.INTERNAL_TOOLS, queued only by library.py): text-to-image, or an edit
        # from references: first the still of the same person (the "same person, new situation" path), then any
        # product photos.
        refs = list(urls[:4])
        if p.get("ref_job"):
            refs.insert(0, _job_output_url(conn, job, p["ref_job"]))
        elif p.get("ref_static"):
            refs.insert(0, _static_remote(conn, p["ref_static"]))
        inp = {"prompt": job["brief"] or "", "resolution": p.get("resolution") or catalog.LIBRARY_STILL_DEFAULT[1],
               "aspect_ratio": p.get("aspect") or "9:16", "quality": p.get("quality") or catalog.LIBRARY_STILL_DEFAULT[0],
               "enhance_prompt": False}
        if refs:
            inp["image_urls"] = refs
        return inp
    if tool == "ugc_video" and p.get("voice_sample"):
        # A presenter's voice sample (library.py): a few seconds of them talking, from their still, no product.
        still = _job_output_url(conn, job, p["ref_job"]) if p.get("ref_job") else \
            _static_remote(conn, p["ref_static"]) if p.get("ref_static") else None
        if not still:
            raise hf.GenError("A voice sample starts from the presenter's still.")
        return {"prompt": catalog.voice_sample_prompt(p.get("presenter") or "", p.get("voice") or "",
                                                      p.get("situation") or "", job["brief"] or ""),
                "image_urls": [still], "duration": int(p.get("duration") or 6), "aspect_ratio": p.get("aspect") or "9:16",
                "resolution": p.get("resolution") or "480p", "bitrate_mode": "high", "generate_audio": True}
    if tool == "ugc_video":
        if not urls:
            raise hf.GenError("Pick a product photo first.")
        import presenters
        import presets
        prod = conn.execute("SELECT name FROM products WHERE id=?", (job["product_id"],)).fetchone() \
            if "product_id" in job.keys() and job["product_id"] else None
        typ = p.get("ad_type") or "ugc"
        pre = presets.get(p.get("preset"))
        rec = pre if pre is not None and pre.kind == "recipe" else None     # a fixed video format (presets.RECIPES)
        still = voice = None
        angles = []
        sid = p.get("situation") or ""
        if typ == "ugc":
            who = p.get("presenter") or ""
            if not sid and presenters.stills_on() and not (p.get("setting") or "").strip() and \
                    presenters.still_path(who, presenters.default_situation(who)):
                # PRESENTER_STILLS with no place picked (the older pages don't send one) and none typed: the
                # presenter's first situation, when its still is approved
                sid = presenters.default_situation(who)
            if p.get("ref_job"):                    # a library job that names the still it starts from
                still = _job_output_url(conn, job, p["ref_job"])
            elif p.get("ref_static"):
                still = _static_remote(conn, p["ref_static"])
            elif presenters.stills_on() and presenters.still_path(who, sid):
                # PRESENTER_STILLS: the approved still of this presenter in this situation is the first reference
                still = _static_remote(conn, presenters.still_path(who, sid))
                if presenters.angles_on():
                    # PRESENTER_ANGLES: the same person from other sides, right after the still, to hold the face
                    angles = [_static_remote(conn, a) for a in presenters.angle_paths(who)]
            if presenters.voice_ref_on() and presenters.voice_path(who) and \
                    (p.get("voice") or presenters.voice_of(who)) == presenters.voice_of(who):
                # PRESENTER_VOICE_REF: their voice sample as the audio reference (not when the member picked another voice)
                voice = _static_remote(conn, presenters.voice_path(who))
        said = job["brief"] or ""
        brow = conn.execute("SELECT * FROM brands WHERE id=? AND workspace_id=?", (job["brand_id"], job["workspace_id"])) \
            .fetchone() if "brand_id" in job.keys() and job["brand_id"] else None
        if brow is not None and "say_as" in brow.keys() and brow["say_as"]:
            import motion_edit
            said = motion_edit.spoken(said, brow["brand_name"] or "", brow["say_as"])    # how to say the brand
        prompt = catalog.talking_prompt(typ, p.get("product") or (prod["name"] if prod else ""),
                                        said, p.get("presenter") or "", p.get("voice") or "",
                                        p.get("setting") or "",
                                        p.get("placement") or (rec.extra.get("placement") if rec else "") or "",
                                        situation=sid,
                                        direction=(rec.extra.get("direction") if rec else "") or "",
                                        still=bool(still), voice_ref=bool(voice), angles=len(angles))
        inp = {"prompt": prompt, "image_urls": ([still] if still else []) + angles + urls[:4],
               "duration": int(p.get("duration") or 10),
               "aspect_ratio": p.get("aspect") or "9:16", "resolution": p.get("resolution") or "720p",
               "bitrate_mode": "high",
               "generate_audio": True}
        if voice:
            inp["audio_urls"] = [voice]
        return inp
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
    if msg.lower().rstrip(".").endswith(("credits returned", "it wasn't counted")):
        return msg      # image_qa.FAIL_TEXT already says so; a free fix (free_fix.FAIL_TEXT) had no credits to refund
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
    if job["tool"] == "ad_image":
        try:
            import free_fix
            import image_qa
            free_fix.release(conn, job["id"])     # a free fix that failed can be asked for again
            image_qa.discard(conn, job["id"])     # pictures held back for a retry are not kept
        except Exception:  # noqa: BLE001 — bookkeeping never undoes the refund
            log.exception("job %s: cleanup after a failure didn't finish", job["id"])
    elif _keyframe_job(job):
        import free_fix
        import keyframe_video
        try:
            free_fix.release(conn, job["id"])     # a one-beat fix that failed can be asked for again
        except Exception:  # noqa: BLE001
            log.exception("job %s: releasing its fix didn't finish", job["id"])
        _pipe(job).cleanup(conn, job)             # drop what is still being made at the vendor and what was fetched
    conn.commit()
    _notify_ready(conn, job)      # the last job of a first batch failing still means the others are ready
    return True


def _note_refusal(conn, job, why: str):
    """Keep what the vendor said about a refused or failed request on the job (params.vendor_reason), for the
    admin's library page. Never raises; the member's message is ours."""
    if not why:
        return
    try:
        p = json.loads(job["params_json"] or "{}")
        p["vendor_reason"] = str(why)[:300]
        conn.execute("UPDATE jobs SET params_json=? WHERE id=?", (json.dumps(p), job["id"]))
        conn.commit()
    except Exception:  # noqa: BLE001 — a note never changes what happens to the job
        log.exception("job %s: couldn't keep the vendor's reason", job["id"])


NO_REASON = "The vendor's content filter refused it and gave no reason."


def _soften(conn, job, why: str) -> bool:
    """A library still of "the same person, somewhere new" that the vendor's content filter refused goes back to the
    queue ONCE with a softer prompt (presenters.still_prompt(soft=True): the person isn't described again, the
    clothes lose their adjectives, the place is one phrase). The vendor doesn't charge a refused request, so the
    job's cost is unchanged. True when it was requeued; False when this isn't that kind of job or it already had
    its second try (the caller then fails and refunds it)."""
    if job["tool"] != "library_still":
        return False
    try:
        p = json.loads(job["params_json"] or "{}")
    except (ValueError, TypeError):
        return False
    lib = p.get("library") if isinstance(p.get("library"), dict) else {}
    if p.get("softened") or lib.get("kind") != "presenter_still" or not (p.get("ref_job") or p.get("ref_static")):
        return False
    import presenters
    try:
        who, where = lib.get("presenter"), lib.get("situation")
        brief = presenters.portrait_prompt(who, True, soft=True) if where == "portrait" else \
            presenters.still_prompt(who, where, same_person=True, soft=True)
    except KeyError:
        return False
    p.update(softened=True, vendor_reason=(why or NO_REASON)[:300])
    t = db.now()
    cur = conn.execute("UPDATE jobs SET status='pending', request_id=NULL, submit_key=NULL, claimed_at=NULL, attempts=0, "
                       "error=NULL, brief=?, params_json=?, updated_at=? WHERE id=? AND status='running'",
                       (brief, json.dumps(p), t, job["id"]))
    conn.commit()
    if cur.rowcount == 1:
        log.info("job %s: the content filter refused the still (%s); trying once more with a softer prompt",
                 job["id"], (why or "no reason given")[:120])
    return cur.rowcount == 1


def _keyframe_job(job) -> bool:
    """Is this a video made in parts (keyframe_video.py, or a story ad: story_video.py)? A cheap look first: most
    jobs never import the modules."""
    if job["tool"] == "story_video":
        return True
    if job["tool"] != "ugc_video" or '"pipeline"' not in (job["params_json"] or ""):
        return False
    import keyframe_video
    return keyframe_video.is_job(job)


def _pipe(job):
    """The module that makes this video in parts: its start, advance, cleanup and can_cancel."""
    if job["tool"] == "story_video":
        import story_video
        return story_video
    import keyframe_video
    return keyframe_video


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
            _auto_edit(conn, job, path)
            make_poster(path)
        conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                     (job["id"], o["kind"], o["url"], path))
    conn.commit()


def _auto_edit(conn, job, path: str) -> bool:
    """AUTO_EDIT (motion_edit.py): captions, hook, brand pop-ins, scenes, end card and sound over a finished
    presenter video, when the batch was made with it on. The raw take stays beside it; a failure keeps the take."""
    if '"auto_edit"' not in (job["params_json"] or ""):
        return False
    import motion_edit
    return motion_edit.wanted(job) and motion_edit.edit_job_output(conn, job, path)


def _images_path(job) -> bool:
    """Does this finished job go through _finish_images? An image ad that has a text layer to set, or any image
    ad while the automatic check is on. Everything else is saved exactly as before."""
    if job["tool"] != "ad_image" or _is_preview(job):
        return False
    try:
        p = json.loads(job["params_json"] or "{}")
    except (ValueError, TypeError):
        return False
    import image_qa
    return bool(p.get("layout")) or image_qa.on()


def _finish_images(conn, job, result):
    """A finished image job, picture by picture (image_qa.py, textlayer.py):

      1. each picture is saved, then looked at by the automatic check when IMAGE_QA is on;
      2. the first time some fail, the ones that passed are held back and the failed ones are made again, once,
         silently: the job goes back to the queue for just that many, with a new idempotency key;
      3. after that, what passed is delivered (with its text layer set, for a layered static); pictures that
         failed twice are dropped and their share of the credits comes back; when nothing passed the job ends
         with image_qa.FAIL_TEXT and a full refund.

    A picture the check couldn't look at (no key, the daily budget, no local copy) counts as passed.

    A layered static has one more check on its first try, with or without IMAGE_QA: when the image model drew a
    person where the words go and no part of the layout can be moved off them (textlayer.words_on_people, a local
    heuristic), the picture is made again once like a failed one. On the second try that heuristic alone never
    drops a picture: only the vision check can. That check always runs on a layered static's text-free picture,
    before any layer is drawn, for two things (image_qa.LAYERED_CHECKS): words the image model painted in by itself
    and a person in a product-only picture. IMAGE_QA adds the rest (product, label, hands)."""
    import image_qa
    import storage
    p = json.loads(job["params_json"] or "{}")
    outs = [o for o in hf.outputs(result) if o["kind"] != "video"]
    if not outs:
        raise hf.GenError("finished with no files")
    attempt = int(p.get("qa_attempt") or 1)
    want = int(p.get("qa_want") or p.get("batch") or 1)
    qa_on = image_qa.on()
    # A static with a text layer: its text-free picture is always checked for words the image model painted in and
    # for a person in it (image_qa.LAYERED_CHECKS), with IMAGE_QA off too; the rest of the check is the switch's.
    check_on = qa_on or bool(p.get("layout"))
    only = None if qa_on else image_qa.LAYERED_CHECKS
    folder = os.path.join(MEDIA, str(job["workspace_id"]), str(job["id"]))
    os.makedirs(folder, exist_ok=True)
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (job["workspace_id"],)).fetchone()
    used = storage.used_bytes(job["workspace_id"], conn)
    passed, failed = [], []
    kit = None
    if p.get("layout") and attempt == 1:
        import textlayer
        brow = conn.execute("SELECT * FROM brands WHERE id=?", (job["brand_id"],)).fetchone() if job["brand_id"] else None
        kit = textlayer.kit_of(brow)
    for i, o in enumerate(outs, start=1):
        ext = ".png"
        if "." in o["url"].rsplit("/", 1)[-1].split("?")[0]:
            ext = "." + o["url"].rsplit("/", 1)[-1].split("?")[0].rsplit(".", 1)[-1][:5]
        path = os.path.join(folder, f"campaignify-{job['id']}-{attempt}{i}{ext}")
        try:
            storage.guard(ws, 0, used)
            r = requests.get(o["url"], timeout=300)
            r.raise_for_status()
            storage.guard(ws, len(r.content), used)
            with open(path, "wb") as f:
                f.write(r.content)
            used += len(r.content)
        except Exception:  # noqa: BLE001 — no local copy: the vendor's link is kept, as before
            path = None
        if kit is not None and path and _words_on_people(path, p, kit):
            # a person where the words go, and nothing to move: made again once (no vision call spent on it)
            res = {"verdict": "fail", "row": image_qa.record(conn, job, p, attempt, i, "fail", [textlayer.PEOPLE_REASON],
                                                              {"words_clear_of_people": False}, "textlayer", 0.0,
                                                              o["url"], path)}
        else:
            res = image_qa.check_output(conn, job, p, attempt, i, o["url"], path, only) if check_on else \
                {"verdict": "pass", "row": None}
        (failed if res["verdict"] == "fail" else passed).append({"url": o["url"], "path": path, "row": res.get("row"),
                                                                 "idx": i})
    conn.commit()
    for c in failed:
        _rm(c["path"])
    if failed and attempt == 1:
        # one silent retry of just the failed ones; the passed ones wait (asset_qa.kept)
        for c in passed:
            if not c["row"]:        # IMAGE_QA is off: a picture that waits still needs its row to wait in
                c["row"] = image_qa.record(conn, job, p, attempt, c["idx"], "skipped", ["not checked"], url=c["url"],
                                           path=c["path"])
            if c["row"]:
                conn.execute("UPDATE asset_qa SET kept=1 WHERE id=?", (c["row"],))
        p.update(qa_attempt=2, qa_want=want, batch=len(failed))
        t = db.now()
        extra = catalog.cost_usd("ad_image", batch=len(failed))
        cur = conn.execute("UPDATE jobs SET status='pending', request_id=NULL, submit_key=NULL, claimed_at=NULL, "
                           "attempts=0, error=NULL, params_json=?, cost_usd=cost_usd+?, updated_at=? "
                           "WHERE id=? AND status='running'", (json.dumps(p), round(extra, 4), t, job["id"]))
        conn.commit()
        if cur.rowcount != 1:          # canceled meanwhile: nothing is kept
            for c in passed:
                _rm(c["path"])
            image_qa.discard(conn, job["id"])
            conn.commit()
        else:
            log.info("job %s: %s of %s picture(s) failed the check; making them again once", job["id"], len(failed),
                     len(outs))
        return
    held = [{"url": r["url"], "path": r["path"], "row": r["id"]} for r in image_qa.kept(conn, job["id"])]
    keep = (held + passed)[:max(1, want)]
    for c in (held + passed)[len(keep):]:
        _rm(c["path"])
    if not keep:
        import free_fix
        _fail(conn, job, free_fix.FAIL_TEXT if p.get("free_fix") else image_qa.FAIL_TEXT)
        return
    if p.get("layout"):
        import textlayer
        brand = conn.execute("SELECT * FROM brands WHERE id=?", (job["brand_id"],)).fetchone() if job["brand_id"] else None
        kit, text = textlayer.kit_of(brand), textlayer.text_from_params(p)
        try:
            for c in keep:
                textlayer.apply_to_file(c["path"], p["layout"], text, kit, p.get("aspect") or "")
        except textlayer.LayerFail as e:
            for c in keep:
                _rm(c["path"])
                _rm(textlayer.base_path(c["path"]) if c["path"] else None)
            raise hf.GenError("The picture couldn't be saved to set its text on. Try again.") from e
    if not _still_running(conn, job):
        for c in keep:
            _rm(c["path"])
        image_qa.discard(conn, job["id"])
        conn.commit()
        return
    for c in keep:
        conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                     (job["id"], "image", "" if p.get("layout") else c["url"], c["path"]))
    conn.execute("UPDATE asset_qa SET kept=0 WHERE job_id=?", (job["id"],))
    conn.commit()
    if not _finish_done(conn, job):
        _drop_outputs(conn, job)
        return
    if len(keep) < want and job["credits"]:
        # some pictures failed twice: their share of the price comes back
        back = job["credits"] - catalog.price_credits("ad_image", batch=len(keep))
        if back > 0:
            db.refund_part(conn, job, back, "refund: image check")
            conn.execute("UPDATE jobs SET error=? WHERE id=?",
                         (f"{want - len(keep)} of {want} couldn't be made right — {back} credits returned.", job["id"]))
            conn.commit()


def _words_on_people(path: str, p: dict, kit: dict) -> bool:
    """textlayer.words_on_people for a job's picture; a check that can't run says no (the picture is kept)."""
    try:
        import textlayer
        return textlayer.words_on_people(path, p.get("layout") or "", textlayer.text_from_params(p), kit,
                                         p.get("aspect") or "")
    except Exception:  # noqa: BLE001 — a heuristic never stops an ad
        log.exception("the words-on-a-person check couldn't run for %s", path)
        return False


def _rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


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
        if _keyframe_job(job):
            # a keyframe-first video: planned here and marked running; its parts (the keyframe, then every beat)
            # are sent and followed by keyframe_video.advance() on each pass
            _pipe(job).start(conn, job)
            return True
        inputs = build_inputs(conn, job)
        model = catalog.model_for(job["tool"])
        # a talking video, and the library's token-priced still: refused when the vendor's own estimate has moved
        if job["tool"] in ("ugc_video", "library_still") and not price_ok(conn, job, model, inputs):
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
    if _keyframe_job(job):
        if not _pipe(job).can_cancel(conn, job):             # its beats are at the vendor, and paid for
            return False, "It's already being made and can't be stopped now."
        if _fail(conn, job, CANCELED):                        # only its keyframe was started: _fail drops it
            return True, failure_text(CANCELED)
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
            held = waits_on(conn, job)
            if held == "wait":      # it starts from another job's picture that isn't finished yet
                continue
            if held == "failed":
                _fail(conn, job, "The still this one starts from wasn't made, so this one wasn't started.")
                changed += 1
                continue
            tried += 1
            with _busy("vendor submit"):   # upload + submit with retries: minutes when the vendor is slow (N2)
                if _submit_one(conn, job):
                    changed += 1
        for job in conn.execute("SELECT * FROM jobs WHERE status='running' ORDER BY id LIMIT 20").fetchall():
            _beat()
            if _keyframe_job(job):
                # a keyframe-first video has no single request to ask about: one bounded step of its pipeline
                try:
                    with _busy("keyframe video"):
                        if _pipe(job).advance(conn, job):
                            changed += 1
                except hf.GenError as e:
                    if _fail(conn, job, str(e)):
                        changed += 1
                except Exception:  # noqa: BLE001 — again next pass; advance() ends a job that never finishes
                    log.exception("job %s: its video pipeline step failed", job["id"])
                continue
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
                    if _images_path(job):
                        with _busy("checking images"):      # the automatic check and the text layer (quality wave 2)
                            _finish_images(conn, job, res)
                        changed += 1
                        continue
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
                why = hf.refusal(res)
                # a library still made from a reference gets one more try with a softer prompt before it is given up
                if not _soften(conn, job, why):
                    _note_refusal(conn, job, why or (NO_REASON if job["tool"] == "library_still" else ""))
                    _fail(conn, job, "Blocked by the content filter. Try a different brief or photo.")
                changed += 1
            elif st in hf.FINAL:
                _note_refusal(conn, job, hf.refusal(res))
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
    if cur.rowcount == 1:
        _notify_ready(conn, job)
    return cur.rowcount == 1


def _notify_ready(conn, job):
    """The "your ads are ready" email, when this was the last job of a member's first batch (ready_email.py)."""
    try:
        import ready_email
        ready_email.on_job_settled(conn, job)
    except Exception:  # noqa: BLE001 — never into the worker loop
        log.warning("ads-ready email check failed for job %s", job["id"])


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


def mode() -> str:
    """Where the background loop runs (roadmap M7.2): "process" when docker-compose says so (RS_WORKER_MODE=process,
    set on both services) and the kill switch RS_WORKER_PROCESS isn't off: the worker container (worker_main.py)
    runs it and the web process starts no loop. "thread" otherwise: the loop is a thread in the web process, as before
    (local runs, and RS_WORKER_PROCESS=off in settings.txt, which also leaves the worker container idle)."""
    if (os.environ.get("RS_WORKER_MODE") or "").strip().lower() != "process":
        return "thread"
    if (os.environ.get("RS_WORKER_PROCESS") or "on").strip().lower() in ("0", "off", "false", "no"):
        return "thread"
    return "process"


_build = []


def build_id() -> str:
    """This image's build id (the Dockerfile writes BUILD_ID once per build; "" outside docker). The web and the
    worker container of one release share it, so /health can tell the new worker's heartbeat from the old one's."""
    if not _build:
        try:
            _build.append(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "BUILD_ID")).read().strip()[:64])
        except OSError:
            _build.append("")
    return _build[0]


def background_here() -> bool:
    """Do the hourly plan sync and the nudge loop run in this process? Thread mode: in the web process, as before.
    Process mode: only in the worker process (RS_PROC_ROLE=worker), never in gunicorn."""
    return mode() == "thread" or (os.environ.get("RS_PROC_ROLE") or "").strip().lower() == "worker"


def loop_once():
    """One pass of the background loop: jobs, flows and sends, conversions, the minute sweep, the daily purge, stale
    holds, ad-account sweeps, launches, changes, insights, backups and (process mode) queued AI runs. Never raises."""
    import alerts
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
    import keyframe_video
    keyframe_video.maybe_purge()   # once a day: the kept beats of videos whose free fix has lapsed (never raises)
    import credits
    credits.maybe_release_stale()  # holds left by a crashed AI run go back after 30 min (never raises)
    import adplatforms
    adplatforms.maybe_sweep()      # unfinished ad-account connects lose their token after 1 hour (never raises)
    import launches
    launches.maybe_tick()          # approved launches: the saga that makes the paused campaign (never raises)
    import admanage
    admanage.maybe_tick()          # approved on/off and budget changes that wait on Meta (never raises)
    import adinsights
    adinsights.maybe_tick()        # results of launched campaigns, every 6 hours (never raises)
    import backup
    backup.maybe_run()     # nightly off-site backup in its own thread, once R2 is set (never raises)
    import wsdelete
    wsdelete.maybe_tick()  # workspaces whose owner asked for deletion, once the 7-day grace has passed (hourly)
    if mode() == "process":
        import tasks
        tasks.tick()       # planner, competitor and free-plan runs the web queued (never raises)


def start():
    """The loop as a daemon thread of the web process (thread mode). In process mode the web starts nothing:
    workerproc.run() calls loop_once() on the worker container's main thread."""
    global _thread
    if _thread or os.environ.get("RS_WORKER", "1") != "1" or mode() == "process":
        return

    def loop():
        while True:
            loop_once()
            time.sleep(POLL_SECONDS)

    _thread = threading.Thread(target=loop, name="rs-worker", daemon=True)
    _thread.start()

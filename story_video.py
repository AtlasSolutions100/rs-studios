"""Story ads (Koby, Nov 14 2026): a short ad told as a little story, scene by scene, like "This is Bob. Bob makes
terrible ads. Then Bob found Campaignify." Two looks:

    photo     real-looking photos of one made-up person, each scene a slow push-in with a caption, then an end card
    animated  the same scenes drawn in a bright 3D animated style, each scene a short moving clip

How a story is made (a job of tool "story_video", run by the worker like keyframe_video.py):

    1. plan     at queue time (plan_scenes): the agent writes 3 to 6 scenes for the product and the idea (Claude, the
                heavy work): one invented adult character, a caption of a few words per scene, what each picture
                shows and, for an animated story, how it moves. Captions and pictures pass the claim rules.
    2. scene 0  one still of the first scene from the library's image model, with the product photos as references
    3. scenes   every other scene from the same model, with scene 0 as a reference too, so it is the same person
    4. clips    animated only: each still made into a short silent clip by the image-to-video model
    5. stitch   ffmpeg (stitch.py): each scene to the video's size (a still gets a slow push-in), its caption as
                real type (videotext.caption_png), crossfades, and an end card from the real packshot and the brand
                kit (videotext.end_card). No sound yet: the ad is made to be read with the sound off.

Every vendor request is a video_parts row (kind "scene" for a still, "clip" for a moving one), sent once with its own
key; advance() is a bounded step the worker calls on every pass. A part that fails is made again once at our cost;
a second failure ends the job and the worker refunds it in full. Never a half-made story.

Money: price() is what the member pays, shown before anything starts: the vendor cost of every still and clip at
the markup. Nothing here changes a price, a plan or anything at Stripe."""
import json
import logging
import math
import os
import shutil

import catalog
import db
import hf

log = logging.getLogger("campaignify")
TOOL = "story_video"
STYLES = ("photo", "animated")
SCENES_MIN, SCENES_MAX = 3, 6
DEFAULT_SCENES = {"photo": 5, "animated": 3}
PHOTO_SECONDS = 2.6               # how long each still scene stays on screen
CLIP_SECONDS = 5                  # the image-to-video model's shortest clip
CLIP_RES = "720p"
END_SECONDS = 2.4
STILL_SETTINGS = ("high", "1k")
JOB_HOURS = 3
PART_STUCK_MINUTES = 45
STITCH_FAIL = "The story couldn't be put together."
STYLE_LINE = {
    "photo": "A real candid photo, like a still from a phone video: natural light, true skin texture, a real home or "
             "shop, slightly funny and relatable.",
    "animated": "A bright, polished 3D animated movie still with soft lighting, rounded friendly character design and "
                "rich color, like a frame from a family animated film. Not a real photo.",
}


def size_for(aspect: str) -> tuple:
    return (1080, 1350) if aspect == "4:5" else (1080, 1920)


# ---------------------------------------------------------------- price

def _still_usd() -> float:
    q, r = STILL_SETTINGS
    return catalog.library_still_usd(q, r, refs=3)


def cost_usd(style: str, scenes: int) -> float:
    n = max(SCENES_MIN, min(SCENES_MAX, int(scenes)))
    usd = n * _still_usd()
    if style == "animated":
        usd += n * CLIP_SECONDS * catalog.resolutions("photo_to_video").get(CLIP_RES, 0.4622)
    return round(usd, 4)


def price(style: str, scenes: int) -> int:
    """Credits for a story of this look and length: the vendor cost at the markup, rounded up."""
    return int(math.ceil(round(cost_usd(style, scenes) * catalog.markup_for(TOOL) * 100, 6)))


def name_of(style: str) -> str:
    return "Animated story ad" if style == "animated" else "Funny story ad"


# ---------------------------------------------------------------- planning (the heavy, Claude part)

PLAN_SYSTEM = (
    "You write short, funny story ads for small businesses, told in scenes like a comic strip. The pattern that works: "
    "meet a relatable made-up person, show their problem in a funny way, the product changes things, a happy ending. "
    "Rules: one main character, an invented adult (never a real or famous person, never a child). Give them a first "
    "name and describe how they look once, in detail, so every picture can draw the same person. Each scene has a "
    "caption of 2 to 5 plain words, in capitals, that a third grader can read (THIS IS BOB. / BOB MADE BAD ADS.). "
    "The product appears in at least two scenes, looking exactly like its photo. Only use facts about the product "
    "that are given; never promise results, money, health or weight changes. No other brand names or logos. "
    "Describe each picture as one clear moment with a simple background. For an animated story also say in a few "
    "words how the scene moves.")
PLAN_SCHEMA = {
    "type": "object", "required": ["character", "scenes"],
    "properties": {
        "character": {"type": "string"},
        "scenes": {"type": "array", "minItems": SCENES_MIN, "maxItems": SCENES_MAX, "items": {
            "type": "object", "required": ["caption", "picture"],
            "properties": {"caption": {"type": "string"}, "picture": {"type": "string"}, "motion": {"type": "string"}}}},
        "cta": {"type": "string"}}}


def plan_scenes(product, idea: str, style: str, scenes: int, usage=None) -> dict:
    """The story for this product and idea: {"character", "scenes": [{caption, picture, motion}], "cta"}. Raises
    ValueError when it can't be written or a line breaks the claim rules."""
    import compliance
    import llm
    facts = [f"Product: {product['name']}"]
    for key in ("description", "price", "offer", "claims", "audience"):
        if product[key]:
            facts.append(f"{key.title()}: {' '.join(str(product[key]).split())[:500]}")
    prompt = ("\n".join(facts) + f"\n\nThe idea: {idea or 'a funny story about someone who needs this product'}\n"
              f"Look: {style}\nScenes: exactly {scenes}, the last one is the happy ending with the product.")
    out = llm.structured(system=PLAN_SYSTEM, prompt=prompt, schema=PLAN_SCHEMA, max_tokens=1600, usage=usage,
                         temperature=0.8)
    character = " ".join(str(out.get("character") or "").split())[:400]
    rows = []
    for sc in (out.get("scenes") or [])[:SCENES_MAX]:
        cap = " ".join(str(sc.get("caption") or "").split())[:40].upper()
        pic = " ".join(str(sc.get("picture") or "").split())[:500]
        if not cap or not pic:
            continue
        rows.append({"caption": cap, "picture": pic, "motion": " ".join(str(sc.get("motion") or "").split())[:200]})
    if len(rows) < SCENES_MIN or not character:
        raise ValueError("The story didn't come back. Try again.")
    for text in [character] + [r["caption"] + " " + r["picture"] for r in rows]:
        v = compliance.check(text)
        if not v["ok"]:
            raise ValueError("The story had a line your ads can't say: " + v["blocks"][0])
    cta = " ".join(str(out.get("cta") or "").split())[:30]
    return {"character": character, "scenes": rows, "cta": cta if cta and compliance.check(cta)["ok"] else ""}


# ---------------------------------------------------------------- the job

def is_job(job) -> bool:
    return job["tool"] == TOOL


def _params(job) -> dict:
    try:
        p = json.loads(job["params_json"] or "{}")
        return p if isinstance(p, dict) else {}
    except ValueError:
        return {}


def parts_dir(job) -> str:
    import worker
    return os.path.join(worker.MEDIA, str(job["workspace_id"]), str(job["id"]), "parts")


def _latest(conn, job_id: int) -> dict:
    out = {}
    for r in conn.execute("SELECT * FROM video_parts WHERE job_id=? ORDER BY kind, idx, attempt", (job_id,)).fetchall():
        out[(r["kind"], r["idx"])] = r
    return out


def _set(conn, part_id: int, **kw):
    kw["updated_at"] = db.now()
    conn.execute(f"UPDATE video_parts SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), part_id))
    conn.commit()


def _add(conn, job, kind: str, idx: int, attempt: int, model: str, inputs: dict, seconds: float, usd: float):
    t = db.now()
    pid = conn.execute(
        "INSERT INTO video_parts (workspace_id, job_id, kind, idx, attempt, status, model, inputs_json, submit_key, "
        "seconds, usd, created_at, updated_at) VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
        (job["workspace_id"], job["id"], kind, idx, attempt, model, json.dumps(inputs), hf.new_key(), seconds,
         round(usd, 4), t, t)).lastrowid
    if attempt > 1:
        conn.execute("UPDATE jobs SET cost_usd=cost_usd+?, updated_at=? WHERE id=?", (round(usd, 4), t, job["id"]))
    conn.commit()
    return conn.execute("SELECT * FROM video_parts WHERE id=?", (pid,)).fetchone()


def _photos(conn, job, p: dict) -> list:
    rows = [conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, job["workspace_id"])).fetchone()
            for pid in p.get("photo_ids") or []]
    return [r for r in rows if r]


def _part_url(conn, part) -> str:
    """A finished still as a URL a model can read: uploaded once from its saved file."""
    if part["remote_url"]:
        return part["remote_url"]
    if part["url"]:
        return part["url"]
    with open(part["path"], "rb") as f:
        url = hf.upload(f.read(), "image/png")
    _set(conn, part["id"], remote_url=url)
    return url


def scene_inputs(conn, job, p: dict, i: int, first_url: str | None) -> tuple:
    import worker
    st = p["story"]
    photos = [worker._remote_url(conn, "photos", ph) for ph in _photos(conn, job, p)][:2]
    sc = st["scenes"][i]
    same = (" The person is exactly the same person as in the first reference image: same face, hair, body and "
            "clothes.") if first_url else ""
    prompt = (f"{STYLE_LINE[st['style']]} Main character: {st['character']}.{same} Scene: {sc['picture']} "
              f"If the product appears, it looks exactly like the product photo reference, label readable. "
              "No words, captions, letters or logos anywhere in the picture except the product's own label. "
              "Leave the bottom fifth of the frame calm.")
    q, r = STILL_SETTINGS
    aspect = st["aspect"] if st["aspect"] in catalog.LIBRARY_STILL_ASPECTS else "9:16"
    refs = ([first_url] if first_url else []) + photos
    inputs = {"prompt": prompt, "resolution": r, "aspect_ratio": aspect, "quality": q, "enhance_prompt": False,
              "image_urls": refs}
    return catalog.model_for("library_still"), inputs, catalog.library_still_usd(q, r, refs=len(refs))


def clip_inputs(conn, job, p: dict, i: int, still_url: str) -> tuple:
    st = p["story"]
    sc = st["scenes"][i]
    prompt = (f"{STYLE_LINE[st['style']]} Gentle, lively motion: {sc.get('motion') or 'the character reacts and the camera slowly moves in'}. "
              "Keep the same character and the product exactly as they are. No words appear.")
    inputs = {"prompt": prompt, "image_url": still_url, "duration": CLIP_SECONDS, "resolution": CLIP_RES,
              "bitrate_mode": "high", "generate_audio": False}
    rate = catalog.resolutions("photo_to_video").get(CLIP_RES, 0.4622)
    return catalog.TOOLS["photo_to_video"]["model"], inputs, CLIP_SECONDS * rate


def start(conn, job) -> None:
    """A claimed, pending story: checked and marked running; advance() sends its parts."""
    import stitch
    p = _params(job)
    if not stitch.have():
        raise hf.GenError("This kind of video can't be made right now. We've been told.")
    st = p.get("story")
    if not isinstance(st, dict) or not st.get("scenes"):
        raise hf.GenError("This story's plan is missing.")
    if not _photos(conn, job, p):
        raise hf.GenError("Pick a product photo first.")
    st["started_at"] = db.now()
    cur = conn.execute("UPDATE jobs SET status='running', request_id=NULL, claimed_at=NULL, error=NULL, params_json=?, "
                       "updated_at=? WHERE id=? AND status='pending'", (json.dumps(p), db.now(), job["id"]))
    conn.commit()
    if cur.rowcount != 1:
        raise hf.GenError("It couldn't start.")


def _submit(conn, part) -> None:
    try:
        rid = hf.submit(part["model"], json.loads(part["inputs_json"] or "{}"), idempotency_key=part["submit_key"])
    except hf.Transient as e:
        if part["tries"] + 1 < 3:
            _set(conn, part["id"], tries=part["tries"] + 1, error=str(e)[:300])
        else:
            _set(conn, part["id"], status="failed", tries=part["tries"] + 1, error=str(e)[:300])
        return
    except hf.GenError as e:
        _set(conn, part["id"], status="failed", error=str(e)[:300])
        return
    _set(conn, part["id"], status="running", request_id=rid, error=None)


def _poll(conn, job, part) -> bool:
    import keyframe_video
    import worker
    old = part["updated_at"] < worker._minutes_ago(PART_STUCK_MINUTES)
    try:
        res = hf.status(part["request_id"])
    except Exception:  # noqa: BLE001 — again next pass, but not for ever
        if old:
            _set(conn, part["id"], status="failed", error="Timed out.")
            return True
        return False
    st = (res.get("status") or "").lower()
    if st == "completed":
        want = "image" if part["kind"] == "scene" else "video"
        o = next((x for x in hf.outputs(res) if x["kind"] == want), None)
        if o is None:
            _set(conn, part["id"], status="failed", error="finished with no file")
            return True
        ext = ".png" if want == "image" else ".mp4"
        path = os.path.join(parts_dir(job), f"{part['kind']}-{part['idx']}-{part['attempt']}{ext}")
        try:
            keyframe_video._download(o["url"], path)
        except hf.GenError:
            raise
        except Exception as e:  # noqa: BLE001
            _set(conn, part["id"], status="failed", error=f"couldn't be fetched: {str(e)[:120]}")
            return True
        _set(conn, part["id"], status="done", url=o["url"], path=path)
        return True
    if st == "nsfw":
        _set(conn, part["id"], status="failed", error="content filter: " + (hf.refusal(res) or "no reason given"))
        return True
    if st in hf.FINAL:
        _set(conn, part["id"], status="failed", error="generation failed: " + (hf.refusal(res) or "no reason given"))
        return True
    if old:
        hf.cancel(part["request_id"])
        _set(conn, part["id"], status="failed", error="Timed out.")
        return True
    return False


def _step(conn, job, part) -> bool:
    if part["status"] == "pending":
        _submit(conn, part)
        return True
    if part["status"] == "running":
        return _poll(conn, job, part)
    return False


def _ensure(conn, job, p, kind, i, parts, make) -> tuple:
    """(the part's latest row, changed). Sends it when it isn't there yet, once more when its first try failed;
    raises hf.GenError when its second try failed."""
    part = parts.get((kind, i))
    if part is None or (part["status"] == "failed" and part["attempt"] < 2):
        model, inputs, usd = make()
        part = _add(conn, job, kind, i, 1 if part is None else part["attempt"] + 1, model, inputs,
                    CLIP_SECONDS if kind == "clip" else 0, usd)
        _submit(conn, part)
        return conn.execute("SELECT * FROM video_parts WHERE id=?", (part["id"],)).fetchone(), True
    if part["status"] == "failed":
        raise hf.GenError("One scene of the story couldn't be made. You weren't charged.")
    changed = _step(conn, job, part)
    return conn.execute("SELECT * FROM video_parts WHERE id=?", (part["id"],)).fetchone(), changed


def advance(conn, job) -> bool:
    """One bounded step: send what can be sent, ask about what is being made, and when every scene is there,
    stitch and deliver. Raises hf.GenError to end the job (the worker refunds it)."""
    import worker
    p = _params(job)
    st = p.get("story") or {}
    if (st.get("started_at") or job["created_at"]) < worker._minutes_ago(JOB_HOURS * 60):
        raise hf.GenError("Timed out.")
    n = len(st.get("scenes") or [])
    parts = _latest(conn, job["id"])
    # scene 0 first: every other scene is drawn from it, so it is the same person
    first, changed = _ensure(conn, job, p, "scene", 0, parts, lambda: scene_inputs(conn, job, p, 0, None))
    if first["status"] != "done":
        return changed
    first_url = None
    waiting = False
    for i in range(1, n):
        if first_url is None:
            first_url = _part_url(conn, first)
        part, ch = _ensure(conn, job, p, "scene", i, parts, lambda i=i: scene_inputs(conn, job, p, i, first_url))
        changed = changed or ch
        waiting = waiting or part["status"] != "done"
    if waiting:
        return changed
    if st.get("style") == "animated":
        parts = _latest(conn, job["id"])
        for i in range(n):
            still = parts[("scene", i)]
            part, ch = _ensure(conn, job, p, "clip", i, parts, lambda i=i, still=still: clip_inputs(conn, job, p, i, _part_url(conn, still)))
            changed = changed or ch
            waiting = waiting or part["status"] != "done"
        if waiting:
            return changed
    _stitch(conn, job, p)
    return True


def _stitch(conn, job, p: dict) -> None:
    import stitch
    import textlayer
    import videotext
    import worker
    st = p["story"]
    parts = _latest(conn, job["id"])
    work = os.path.join(parts_dir(job), "work")
    os.makedirs(work, exist_ok=True)
    size, fps, aspect = size_for(st["aspect"]), 30, st["aspect"]
    brand = conn.execute("SELECT * FROM brands WHERE id=?", (job["brand_id"],)).fetchone() if job["brand_id"] else None
    prod = conn.execute("SELECT * FROM products WHERE id=?", (job["product_id"],)).fetchone() if job["product_id"] else None
    kit = textlayer.kit_of(brand)
    photos = _photos(conn, job, p)
    packshot = next((ph["path"] for ph in photos if ph["path"] and os.path.exists(ph["path"])), None)
    safe_bottom = textlayer.SAFE.get(aspect, textlayer.SAFE["9:16"])[3]
    try:
        clips = []
        for i, sc in enumerate(st["scenes"]):
            cap = os.path.join(work, f"cap-{i}.png")
            videotext.caption_png(sc["caption"], size, cap, kit, aspect)
            still = parts[("scene", i)]["path"]
            dst = os.path.join(work, f"scene-{i}.mp4")
            if st["style"] == "animated":
                secs = float(CLIP_SECONDS)
                stitch.render_beat(parts[("clip", i)]["path"], dst, size=size, fps=fps, seconds=secs,
                                   overlays=[{"path": cap, "start": 0, "end": secs}], safe_bottom=safe_bottom,
                                   loudnorm=False)
            else:
                secs = PHOTO_SECONDS
                base = os.path.join(work, f"base-{i}.mp4")
                stitch.still_clip(still, base, size=size, fps=fps, seconds=secs)
                stitch.render_beat(base, dst, size=size, fps=fps, seconds=secs,
                                   cutaways=[{"path": still, "start": 0, "seconds": secs, "kind": "still"}],
                                   overlays=[{"path": cap, "start": 0.15, "end": secs}], safe_bottom=safe_bottom,
                                   loudnorm=False)
            clips.append(dst)
        card = os.path.join(work, "end-card.png")
        badge = ((prod["offer"] or prod["price"] or "").strip() if prod is not None else "")
        videotext.end_card(size, card, packshot=packshot, kit=kit, aspect=aspect,
                           headline=(prod["name"] if prod is not None else ""),
                           subline="" if kit.get("logo") else (kit.get("brand") or ""), badge=badge,
                           cta=st.get("cta") or videotext.cta_for(""))
        end = os.path.join(work, "end-card.mp4")
        stitch.still_clip(card, end, size=size, fps=fps, seconds=END_SECONDS)
        clips.append(end)
        out_dir = os.path.join(worker.MEDIA, str(job["workspace_id"]), str(job["id"]))
        final = os.path.join(out_dir, f"campaignify-{job['id']}-1.mp4")
        info = stitch.concat(clips, final, fps=fps, crossfade_frames=5)
    except stitch.StitchError as e:
        log.warning("job %s: the story couldn't be stitched: %s", job["id"], str(e)[:300])
        raise hf.GenError(STITCH_FAIL) from e
    if not worker._still_running(conn, job):
        cleanup(conn, job)
        return
    worker.make_poster(final)
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (job["id"], "video", "", final))
    conn.commit()
    if not worker._finish_done(conn, job):
        worker._drop_outputs(conn, job)
        return
    log.info("job %s: a %s story of %s scenes, %.1f s", job["id"], st["style"], len(st["scenes"]), info["duration"])
    shutil.rmtree(parts_dir(job), ignore_errors=True)


def cleanup(conn, job) -> None:
    """A story that ended without a video: what is still being made is dropped at the vendor if it can be, and
    what was fetched is removed."""
    for r in conn.execute("SELECT * FROM video_parts WHERE job_id=? AND status IN ('pending', 'running')", (job["id"],)):
        if r["request_id"]:
            try:
                hf.cancel(r["request_id"])
            except Exception:  # noqa: BLE001
                pass
        _set(conn, r["id"], status="failed", error="the story ended")
    shutil.rmtree(parts_dir(job), ignore_errors=True)


def can_cancel(conn, job) -> bool:
    """Only before any scene is at the vendor."""
    return not conn.execute("SELECT 1 FROM video_parts WHERE job_id=? AND status IN ('running', 'done')",
                            (job["id"],)).fetchone()


def queue(conn, product, idea: str, style: str, scenes: int, aspect: str = "9:16", credits: int | None = None):
    """Write the story (Claude) and queue its job with its credits taken (core.queue_job). Returns the job id, or
    raises ValueError with what to tell the member. Call inside a request (g.ws, g.user)."""
    from flask import g

    import core
    import llm
    style = style if style in STYLES else "photo"
    scenes = max(SCENES_MIN, min(SCENES_MAX, int(scenes or DEFAULT_SCENES[style])))
    aspect = aspect if aspect in ("9:16", "4:5") else "9:16"
    photo = product["primary_photo_id"] or (conn.execute("SELECT id FROM photos WHERE product_id=? ORDER BY id LIMIT 1",
                                                         (product["id"],)).fetchone() or {"id": None})["id"]
    if not photo:
        raise ValueError("Add a photo of the product first.")
    if not llm.configured():
        raise ValueError("Story ads aren't available right now.")
    story = plan_scenes(product, idea, style, scenes)
    story.update(style=style, aspect=aspect)
    cost = price(style, len(story["scenes"]))          # a shorter story than asked for costs less, never more
    if credits is not None and cost > credits:
        raise ValueError(f"The price changed to {cost:,} credits. Check it and confirm again.")
    params = {"pipeline": "story", "photo_ids": [photo], "story": story, "aspect": aspect}
    jid = core.queue_job(conn, TOOL, None, (idea or name_of(style))[:300], params, cost_usd(style, len(story["scenes"])),
                         cost, quiet=True, product_id=product["id"])
    if not jid:
        raise ValueError(g.get("verify_blocked") or "There aren't enough credits for this story.")
    return jid

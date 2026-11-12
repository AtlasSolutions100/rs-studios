"""The keyframe-first video pipeline (m15). Switch: KEYFRAME_VIDEO, default off (presets.keyframe_on()).

A talking video made from a recipe (presets.RECIPES) used to be one long unbroken take. With the switch on it is
built from ONE approved still, in beats, and stitched:

    1. keyframe   one still of the whole scene, made by the library's image model from references: the presenter's
                  approved situation still (static/presenters/<id>/<situation>.webp; without one the person is
                  drawn from the roster's description) and the member's product photos, the product placed where
                  the recipe says (on the table, in hand, on the shelf). A voiceover's keyframe is the product in
                  its scene. Members never pay for it: the price is the per-duration price it always was.
    2. check      the image check looks at the keyframe in keyframe mode (product there, label right, no extra
                  text, hands and face fine, one clear face), whatever IMAGE_QA says. A failed keyframe is made
                  again once, silently; a second failure ends the job before any video is paid for, with the usual
                  "we couldn't get this one right" and a full refund.
    3. beats      every spoken beat of the recipe is one reference-to-video request with the keyframe as its image
                  reference (and the presenter's voice sample when PRESENTER_VOICE_REF is on), the beat's share of
                  the script as its line, and the realism and fidelity lines. All beats are sent at once. A beat
                  that fails is made again once; after that the whole job is refunded. Never a partial video.
    4. cutaways   in a presenter recipe, a beat that shows the product or hands is still spoken by the presenter:
                  silent b-roll is laid OVER part of its picture while their voice carries on (one continuous
                  voice). The b-roll is an image-to-video clip from the real packshot while the recipe's seconds
                  allow it, else a slow push-in on the packshot itself (free, and the label can't change). A
                  recipe that shows the member's screens (over the product page, show your app) puts their real
                  screenshots full frame with the presenter small in a corner; nothing of a page is generated.
    5. stitch     ffmpeg (stitch.py): every beat brought to one size and rate with its sound levelled (loudnorm),
                  hard cuts (a recipe can ask for a crossfade of a few frames), captions and tags as real type
                  (videotext.py), then an end card from the real packshot and the brand kit, with a quiet tail.
                  A machine without ffmpeg refuses the job when it starts, before anything is bought.
    6. frame check (VIDEO_QA, default off, capped a day): three frames of each beat against the product photo and
                  the keyframe's face. A failing beat is made again once.

How it runs: a job whose params say pipeline = "keyframe_video" is planned once (plan(), kept in params.kf) and
then stays 'running' while its parts are made; each vendor request is a row in video_parts, with its own
idempotency key. worker.tick() calls advance() for such a job on every pass instead of asking the vendor about one
request. advance() does a bounded step and can be called again after a crash: nothing is sent twice.

Money. The member's price never changes (catalog.price_credits for the length they picked). What it costs us is
plan_cost(): beat seconds and cutaway seconds at the vendor's rate, the keyframe, and the checks; cost_table()
lays every recipe's cost against its price and flags thin margins. Two caps: a plan never has more vendor seconds
than keeps PLAN_MARGIN (it limits generated cutaways), and one job's video seconds, re-renders included, never
pass hard_seconds(): about what the member paid for (1.05 x the markup x the seconds sold: 2.1 x at 720p, 1.47 x
at 1080p). Past it a failing beat is refunded, not made again.

A free fix (FREE_FIX) of such a video re-renders only the beat the member points at, from the kept keyframe, and
stitches again with the other beats as they were (free_fix.py).
"""
import json
import logging
import math
import os
import shutil

import requests

import catalog
import db
import hf
import presenters
import presets

log = logging.getLogger("campaignify")
PIPELINE = "keyframe_video"
BEAT_MIN, BEAT_MAX = 4, 8          # the vendor's shortest take; no lip-synced shot runs longer than eight seconds
END_CARD_SECONDS = 2.0
BROLL_SECONDS = 5                  # one generated cutaway: the image-to-video model's shortest clip
BROLL_LEAD, BROLL_TAIL = 0.8, 1.2  # the presenter is on screen this long before a cutaway and after it
FIXED_PLACES = ("worn", "none")    # recipe placements a member's "holding" never replaces (catalog.PRODUCT_AT)
PLAN_MARGIN = 0.30                 # a plan keeps at least this margin: it caps how many seconds are generated
HARD_FACTOR = 1.05                 # one job's vendor seconds, re-renders included: this x the markup x the seconds sold
PART_TRIES = 3                     # tries at a busy vendor before one attempt of a part counts as failed
PART_STUCK_MINUTES = 45
JOB_HOURS = 3
KEEP_DAYS = 14                     # how long a delivered video's beats are kept for its free fix
FRAMES_PER_BEAT = 3
STITCH_FAIL = "The video couldn't be put together."
NO_FFMPEG = "This kind of video can't be made right now. We've been told."
DEFAULT_VIDEO_QA_DAILY_MAX = 3000


def on() -> bool:
    return presets.keyframe_on()


def video_qa_on() -> bool:
    return os.environ.get("VIDEO_QA", "0").strip().lower() in ("1", "on", "true", "yes")


def video_qa_daily_max() -> int:
    try:
        return int(os.environ.get("VIDEO_QA_DAILY_MAX", str(DEFAULT_VIDEO_QA_DAILY_MAX)))
    except ValueError:
        return DEFAULT_VIDEO_QA_DAILY_MAX


def _params(job) -> dict:
    try:
        d = json.loads(job["params_json"] or "{}")
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def is_job(job) -> bool:
    """Is this job rendered by this pipeline? What its params say, never the switch: a job queued while the switch
    was on finishes this way, and one queued while it was off stays one take."""
    raw = job["params_json"] or ""
    return job["tool"] == "ugc_video" and '"pipeline"' in raw and _params(job).get("pipeline") == PIPELINE


# ---------------------------------------------------------------- the plan

def fit_beats(rec, duration: int, beat_seconds: int | None = None) -> list:
    """The recipe's spoken beats for a video of `duration` seconds: copies of its beat records with whole seconds
    that add up to `duration`, each BEAT_MIN to BEAT_MAX. The recipe's own length gives its own beats. Another
    length scales them; a beat that would fall under four seconds joins its neighbour, one over eight is cut in
    two. `beat_seconds` (the library's cheap test) makes every beat that long instead."""
    spoken = [dict(b) for b in rec.beats if b.get("beat") != "end card"]
    if not spoken:
        return []
    if beat_seconds:
        for b in spoken:
            b["seconds"] = max(BEAT_MIN, min(BEAT_MAX, int(beat_seconds)))
        return spoken
    own = sum(int(b["seconds"]) for b in spoken)
    duration = max(BEAT_MIN, int(duration or own))
    if duration != own:
        raw = [b["seconds"] * duration / own for b in spoken]
        secs = [int(math.floor(x)) for x in raw]
        for i in sorted(range(len(raw)), key=lambda i: raw[i] - secs[i], reverse=True)[: duration - sum(secs)]:
            secs[i] += 1
        for b, s in zip(spoken, secs):
            b["seconds"] = s
    while len(spoken) > 1 and min(b["seconds"] for b in spoken) < BEAT_MIN:
        i = min(range(len(spoken)), key=lambda i: spoken[i]["seconds"])
        j = i - 1 if i == len(spoken) - 1 or (i > 0 and spoken[i - 1]["seconds"] <= spoken[i + 1]["seconds"]) else i + 1
        keep, gone = (spoken[j], spoken[i]) if j < i else (spoken[i], spoken[j])
        keep["seconds"] += gone["seconds"]
        spoken.remove(gone)
    out = []
    for b in spoken:
        parts = [b]
        while max(x["seconds"] for x in parts) > BEAT_MAX:
            big = max(parts, key=lambda x: x["seconds"])
            k = parts.index(big)
            first, second = dict(big), dict(big, tag="")
            first["seconds"], second["seconds"] = math.ceil(big["seconds"] / 2), big["seconds"] // 2
            second["beat"] = f"{big['beat']} (more)"
            parts[k:k + 1] = [first, second]
        out += parts
    return out


def _units(script: str, n: int, longest: int) -> list:
    """The script as pieces that can each be said in one beat: its sentences; when there are fewer of those than
    beats, or one is longer than a beat holds, cut at a comma, else between two words."""
    units = catalog.sentences(script)

    def halve(u):
        parts = [x.strip() for x in u.split(", ")]
        if len(parts) > 1:
            mid = min(range(1, len(parts)), key=lambda k: abs(len(" ".join(parts[:k]).split()) - len(u.split()) / 2))
            return [", ".join(parts[:mid]) + ",", ", ".join(parts[mid:])]
        w = u.split()
        return [" ".join(w[: len(w) // 2]), " ".join(w[len(w) // 2:])] if len(w) > 1 else [u]
    guard = 0
    while guard < 60:
        guard += 1
        big = next((u for u in units if len(u.split()) > longest and len(u.split()) > 1), None)
        if big is None and len(units) >= n:
            break
        cut = big or max(units, key=lambda u: len(u.split()), default="")
        if not cut or len(cut.split()) < 2:
            break
        k = units.index(cut)
        units[k:k + 1] = halve(cut)
    return [u for u in units if u.strip()]


def split_script(script: str, seconds: list) -> list:
    """The script cut into one line per beat, in order, each beat getting about its share of the words by its
    length, on sentence ends where it can. A script with fewer words than beats leaves the last lines empty."""
    n = len(seconds)
    if n <= 1:
        return [" ".join((script or "").split())] if n else []
    units = _units(" ".join((script or "").split()), n, catalog.hard_words(min(seconds)))
    total_words, total_secs = sum(len(u.split()) for u in units), float(sum(seconds))
    lines, k, said, clock = [], 0, 0, 0.0
    for i, s in enumerate(seconds):
        clock += s
        mine = []
        left_beats = n - i - 1
        while k < len(units) and len(units) - k > left_beats:
            w = len(units[k].split())
            # take it when this beat is still short of its share, or when it has nothing yet
            if mine and said + w / 2 > total_words * clock / total_secs:
                break
            mine.append(units[k])
            said += w
            k += 1
        if not mine and k < len(units) and len(units) - k >= left_beats + 1:
            mine.append(units[k])
            said += len(units[k].split())
            k += 1
        if i == n - 1:
            mine += units[k:]
            k = len(units)
        lines.append(" ".join(mine))
    return lines


def plan(rec, p: dict, script: str, *, n_photos: int = 1, has_still: bool = False, category: str = "",
         beat_seconds: int | None = None) -> dict:
    """What will be made for this job: the keyframe, the beats with their lines, the cutaways, the words set on it
    and the end card. Deterministic (no model), so the same config always plans the same video."""
    ugc = rec.ad_type == "ugc"
    beats = fit_beats(rec, int(p.get("duration") or rec.price.get("duration") or 8), beat_seconds)
    lines = split_script(script, [b["seconds"] for b in beats])
    while len(beats) > 1 and not lines[-1]:          # fewer words than beats: the last beats join the one before
        gone = beats.pop()
        beats[-1]["seconds"] = min(catalog.TALKING_MAX_SECONDS, beats[-1]["seconds"] + gone["seconds"])
        lines = split_script(script, [b["seconds"] for b in beats])
    sold = sum(b["seconds"] for b in beats)
    res = p.get("resolution") or catalog.DEFAULT_RESOLUTION
    # how many seconds may be generated on top of the beats and still keep PLAN_MARGIN (it decides the cutaways)
    extra = _extra_seconds(sold, res)
    out_beats, cutaways = [], []
    for i, (b, line) in enumerate(zip(beats, lines)):
        src = b.get("source") or ("presenter" if ugc else "product")
        nb = {"i": i, "beat": b["beat"], "seconds": int(b["seconds"]), "source": src, "shot": b.get("shot") or "",
              "line": line, "tag": b.get("tag") or "", "place": b.get("place") or "", "screen": src == "screen"}
        out_beats.append(nb)
        if ugc and src in ("product", "hands"):
            # the presenter says this beat too; the product is shown over part of it
            room = nb["seconds"] - BROLL_LEAD - (BROLL_TAIL if i == len(beats) - 1 else 0.4)
            secs = round(min(BROLL_SECONDS, room), 2)
            if secs >= 1.5:
                kind = "video" if extra >= BROLL_SECONDS and n_photos else "still"
                if kind == "video":
                    extra -= BROLL_SECONDS
                cutaways.append({"beat": i, "kind": kind, "start": BROLL_LEAD, "seconds": secs, "shot": nb["shot"]})
    end = next((b for b in rec.beats if b.get("beat") == "end card"), None)
    first = beats[0].get("source") if beats else ""
    captions = p.get("captions")
    at = rec.extra.get("product_at") or "table"
    if at not in FIXED_PLACES and p.get("placement") == "holding":     # a try-on stays worn, an app stays out of frame
        at = "hand"
    return {"v": 1, "recipe": rec.id, "ad_type": rec.ad_type, "seconds": sold, "resolution": res,
            "aspect": p.get("aspect") or "9:16",
            "keyframe": {"kind": "presenter" if ugc else "scene", "hands": (not ugc) and first == "hands",
                         "product_at": at,
                         "shot": (beats[0].get("shot") if beats and not ugc else "") or "",
                         "refs": (1 if (ugc and has_still) else 0)
                         + (0 if (ugc and at == "none") else min(max(0, n_photos), catalog.KEYFRAME_MAX_PHOTOS))},
            "screen_layout": rec.extra.get("screen_layout") or "pip",
            "beats": out_beats, "cutaways": cutaways,
            "end_card": ({"seconds": END_CARD_SECONDS, "badge": "price or offer" in (end.get("text") or "")}
                         if end is not None else None),
            "captions": bool(rec.extra.get("captions")) if captions is None else bool(captions),
            "labels": list(rec.extra.get("labels") or ()), "crossfade": int(rec.extra.get("crossfade") or 0),
            "category": category}


def _rate(resolution: str) -> float:
    if resolution == "480p":
        return catalog.LIBRARY_USD_PER_SECOND_480P
    return catalog.resolutions("ugc_video").get(resolution) or catalog.resolutions("ugc_video")[catalog.DEFAULT_RESOLUTION]


def broll_resolution(resolution: str) -> str:
    """A cutaway is never rendered above 720p: it is silent b-roll scaled to the frame, and 1080p seconds cost
    two and a half times as much."""
    return "480p" if resolution == "480p" else "720p"


def _markup(resolution: str) -> float:
    return catalog.markup_for("ugc_video", resolution)


def _extra_seconds(sold: int, resolution: str) -> float:
    """Seconds that may be generated beyond the beats while the plan keeps PLAN_MARGIN (counted at the cutaway's
    own rate). About 0.4 x the seconds sold at 720p; nothing at 1080p, whose markup is 1.4."""
    rate, brate = _rate(resolution), _rate(broll_resolution(resolution))
    budget = (1 - PLAN_MARGIN) * _markup(resolution) * sold * rate - sold * rate - 0.30     # 0.30: keyframe and checks
    return max(0.0, budget / brate)


def hard_seconds(sold: float, resolution: str = "") -> float:
    """The most video seconds one job may have made at the vendor, second tries included: about what the member
    paid for. 2.1 x the seconds sold at 720p (one beat can always be made again), 1.47 x at 1080p."""
    return HARD_FACTOR * _markup(resolution or catalog.DEFAULT_RESOLUTION) * sold


def check_usd() -> float:
    import image_qa
    return image_qa.cost()["check_usd"]


def plan_cost(pl: dict, video_qa: bool | None = None) -> dict:
    """What a plan costs us at the vendor's rates: {"usd", "talk_usd", "broll_usd", "keyframe_usd", "qa_usd",
    "talk_seconds", "broll_seconds"}. `video_qa`: count the frame check (default: as the switch is)."""
    res = pl.get("resolution") or catalog.DEFAULT_RESOLUTION
    talk = sum(b["seconds"] for b in pl["beats"])
    broll = sum(BROLL_SECONDS for c in pl["cutaways"] if c["kind"] == "video")
    kf = catalog.library_still_usd(*catalog.KEYFRAME_SETTINGS, refs=pl["keyframe"]["refs"])
    qa = check_usd()
    if video_qa_on() if video_qa is None else video_qa:
        qa += FRAMES_PER_BEAT * len(pl["beats"]) * check_usd() * 1.4          # three pictures in each frame check
    talk_usd, broll_usd = talk * _rate(res), broll * _rate(broll_resolution(res))
    return {"usd": round(talk_usd + broll_usd + kf + qa, 4), "talk_usd": round(talk_usd, 4),
            "broll_usd": round(broll_usd, 4), "keyframe_usd": round(kf, 4), "qa_usd": round(qa, 4),
            "talk_seconds": talk, "broll_seconds": broll}


def cost_table(resolution: str = "720p", video_qa: bool = True) -> list:
    """Every recipe at its own length: what it costs us (planned, and with the one re-render a failed frame check
    can add) against what a member pays, the margins, and the flags. The README's table and the tests read this."""
    out = []
    for rec in presets.RECIPES:
        dur = rec.price["duration"]
        pl = plan(rec, {"duration": dur, "resolution": resolution}, "word " * catalog.max_words(dur), n_photos=1,
                  has_still=rec.ad_type == "ugc")
        c = plan_cost(pl, video_qa)
        credits = catalog.price_credits("ugc_video", duration=dur, resolution=resolution)
        price = credits / 100.0
        longest = max(b["seconds"] for b in pl["beats"])
        again = c["talk_seconds"] + c["broll_seconds"] + longest <= hard_seconds(dur, resolution)
        # the keyframe made twice, and (where the seconds cap lets it) the longest beat made twice
        worst = c["usd"] + c["keyframe_usd"] + (longest * _rate(resolution) if again else 0)
        margin, wmargin = 1 - c["usd"] / price, 1 - worst / price
        flags = []
        if margin < 0:
            flags.append("negative")
        elif margin < 0.30:
            flags.append("under 30%")
        if wmargin < 0:
            flags.append("negative if a beat is made twice")
        elif wmargin < 0.30:
            flags.append("under 30% if a beat is made twice")
        out.append({"id": rec.id, "name": rec.name, "seconds": dur, "beats": len(pl["beats"]),
                    "cutaway_seconds": c["broll_seconds"], "stills": sum(1 for x in pl["cutaways"] if x["kind"] == "still"),
                    "vendor_seconds": c["talk_seconds"] + c["broll_seconds"], "usd": round(c["usd"], 2),
                    "worst_usd": round(worst, 2), "credits": credits, "margin": round(margin, 3),
                    "worst_margin": round(wmargin, 3), "seconds_cap": int(hard_seconds(dur, resolution)),
                    "beat_again": again, "flags": flags})
    return out


# ---------------------------------------------------------------- parts

def parts_dir(job) -> str:
    import worker
    return os.path.join(worker.MEDIA, str(job["workspace_id"]), str(job["id"]), "parts")


def _all(conn, job_id: int) -> list:
    return conn.execute("SELECT * FROM video_parts WHERE job_id=? ORDER BY kind, idx, attempt", (job_id,)).fetchall()


def _latest(conn, job_id: int) -> dict:
    """{(kind, idx): the row of its latest attempt}."""
    out = {}
    for r in _all(conn, job_id):
        out[(r["kind"], r["idx"])] = r
    return out


def _set(conn, part_id: int, **kw):
    kw["updated_at"] = db.now()
    conn.execute(f"UPDATE video_parts SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), part_id))
    conn.commit()


def _add(conn, job, kind: str, idx: int, attempt: int, model: str, inputs: dict, seconds: float, usd: float) -> int:
    t = db.now()
    pid = conn.execute(
        "INSERT INTO video_parts (workspace_id, job_id, kind, idx, attempt, status, model, inputs_json, submit_key, "
        "seconds, usd, created_at, updated_at) VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
        (job["workspace_id"], job["id"], kind, idx, attempt, model, json.dumps(inputs), hf.new_key(), seconds,
         round(usd, 4), t, t)).lastrowid
    if attempt > 1:          # a second try is ours to pay for: it is added to what the job cost us
        conn.execute("UPDATE jobs SET cost_usd=cost_usd+?, updated_at=? WHERE id=?", (round(usd, 4), t, job["id"]))
    conn.commit()
    return pid


def keyframe_of(conn, job_id: int):
    """The approved keyframe of a job (its video_parts row), or None."""
    return conn.execute("SELECT * FROM video_parts WHERE job_id=? AND kind='keyframe' AND status='done' ORDER BY attempt "
                        "DESC LIMIT 1", (job_id,)).fetchone()


def beats_of(job) -> list:
    """[{"n", "name", "seconds", "line"}] of a planned job, 1-based, for a picker ("which part isn't right?")."""
    kf = _params(job).get("kf") or {}
    return [{"n": b["i"] + 1, "name": b["beat"], "seconds": b["seconds"], "line": b["line"]} for b in kf.get("beats") or []]


# ---------------------------------------------------------------- what is sent

def _photos(conn, job, p: dict) -> list:
    rows = [conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, job["workspace_id"])).fetchone()
            for pid in p.get("photo_ids") or []]
    return [r for r in rows if r]


def _screen_paths(conn, job, p: dict, photos: list) -> list:
    """The member's own screenshots for a screen beat: the ones passed with the config, else (an app) the
    product's own pictures."""
    rows = [conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, job["workspace_id"])).fetchone()
            for pid in p.get("screens") or []]
    rows = [r for r in rows if r] or photos
    return [r["path"] for r in rows if r["path"] and os.path.exists(r["path"])]


def no_product(p: dict) -> bool:
    """Is the product kept out of this video's generated pictures (an app or a website: catalog.PRODUCT_AT "none")?
    Its photos are then never sent to a model, and its checks don't look for it (image_qa.py)."""
    kf = p.get("kf") if isinstance(p.get("kf"), dict) else {}
    return kf.get("ad_type") == "ugc" and (kf.get("keyframe") or {}).get("product_at") == "none"


def screen_for(conn, job, p: dict, photos: list, b: dict) -> dict:
    """What a screen beat shows (stitch.render_beat's `screen`): the product's screen recording when the job
    carries one, its share of it for this beat, else the member's screenshots. Raises hf.GenError when it is gone."""
    import recordings
    kf = p["kf"]
    layout = kf.get("screen_layout") or "pip"
    if p.get("recording"):
        rec = recordings.get(conn, job["workspace_id"], p["recording"])
        fmt = recordings.container(rec["path"]) if rec is not None else ""
        if not fmt:
            raise hf.GenError("The screen recording this video shows isn't there any more.")
        shown = [x for x in kf["beats"] if x.get("screen")]
        speed, _ = recordings.window(rec["seconds"], sum(x["seconds"] for x in shown))
        before = sum(x["seconds"] for x in shown if x["i"] < b["i"])
        # a recording shorter than its beats holds its last picture: a later beat never starts past its end
        at = min(before * speed, max(0.0, float(rec["seconds"] or 0) - 1.0))
        return {"video": rec["path"], "format": fmt, "start": round(at, 3), "speed": speed, "layout": layout,
                "size": (rec["width"], rec["height"])}
    shots = _screen_paths(conn, job, p, photos)
    if not shots:
        raise hf.GenError("The screenshot this video shows isn't there any more.")
    return {"images": shots, "layout": layout}


def _still_url(conn, job, p: dict):
    """The presenter's approved still as a URL the image model can read, or None: the one a library job names,
    else the committed file of this presenter in this situation."""
    import worker
    who, sid = p.get("presenter") or "", p.get("situation") or presenters.default_situation(p.get("presenter") or "")
    if p.get("ref_job"):
        return worker._job_output_url(conn, job, p["ref_job"])
    if p.get("ref_static"):
        return worker._static_remote(conn, p["ref_static"])
    path = presenters.still_path(who, sid)
    return worker._static_remote(conn, path) if path else None


def has_still(p: dict) -> bool:
    who, sid = p.get("presenter") or "", p.get("situation") or presenters.default_situation(p.get("presenter") or "")
    return bool(p.get("ref_job") or p.get("ref_static") or presenters.still_path(who, sid))


def _product_name(conn, job, p: dict) -> str:
    if p.get("product"):
        return p["product"]
    row = conn.execute("SELECT name FROM products WHERE id=?", (job["product_id"],)).fetchone() if job["product_id"] else None
    return row["name"] if row else ""


def keyframe_inputs(conn, job, p: dict) -> tuple:
    """(model, inputs, vendor dollars) of the job's keyframe."""
    import worker
    kf = p["kf"]
    photos = _photos(conn, job, p)
    bare = no_product(p)          # an app or a website: the presenter alone, and no picture of it goes to the model
    urls = [] if bare else [worker._remote_url(conn, "photos", ph) for ph in photos][: catalog.KEYFRAME_MAX_PHOTOS]
    if not urls and not bare:
        raise hf.GenError("Pick a product photo first.")
    still = _still_url(conn, job, p) if kf["keyframe"]["kind"] == "presenter" else None
    sid = p.get("situation") or presenters.default_situation(p.get("presenter") or "")
    prompt = catalog.keyframe_prompt(kf["ad_type"], _product_name(conn, job, p), presenter_id=p.get("presenter") or "",
                                     situation=sid, product_at=kf["keyframe"]["product_at"], has_still=bool(still),
                                     shot=kf["keyframe"]["shot"], hands=kf["keyframe"]["hands"],
                                     setting=(p.get("setting") or "").strip())
    q, r = catalog.KEYFRAME_SETTINGS
    aspect = kf["aspect"] if kf["aspect"] in catalog.LIBRARY_STILL_ASPECTS else "9:16"
    refs = ([still] if still else []) + urls
    inputs = {"prompt": prompt, "resolution": r, "aspect_ratio": aspect, "quality": q, "enhance_prompt": False,
              "image_urls": refs}
    return catalog.model_for("library_still"), inputs, catalog.library_still_usd(q, r, refs=len(refs))


def _keyframe_url(conn, k) -> str:
    """The approved keyframe as a URL the video model can read: its saved file, uploaded once."""
    if k["remote_url"]:
        return k["remote_url"]
    if not k["path"] or not os.path.exists(k["path"]):
        if k["url"]:
            return k["url"]
        raise hf.GenError("The keyframe isn't there any more.")
    ext = k["path"].rsplit(".", 1)[-1].lower()
    with open(k["path"], "rb") as f:
        url = hf.upload(f.read(), {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png"))
    _set(conn, k["id"], remote_url=url)
    return url


def beat_inputs(conn, job, p: dict, i: int, kf_url: str) -> tuple:
    """(model, inputs, vendor dollars) of beat i."""
    import worker
    kf = p["kf"]
    b = kf["beats"][i]
    urls = [] if no_product(p) else [worker._remote_url(conn, "photos", ph) for ph in _photos(conn, job, p)][:3]
    who = p.get("presenter") or ""
    voice = None
    if kf["ad_type"] == "ugc" and presenters.voice_ref_on() and presenters.voice_path(who) and \
            (p.get("voice") or presenters.voice_of(who)) == presenters.voice_of(who):
        voice = worker._static_remote(conn, presenters.voice_path(who))
    note = p.get("fix_note") if p.get("fix_of") and int(p.get("fix_beat", -1)) == i else ""
    prompt = catalog.beat_prompt(kf["ad_type"], _product_name(conn, job, p), b["line"], presenter_id=who,
                                 voice_id=p.get("voice") or "", shot=b["shot"] if (kf["ad_type"] != "ugc" or b["source"] == "presenter") else "",
                                 hands=b["source"] == "hands", product_at=kf["keyframe"]["product_at"],
                                 new_place=b.get("place") or "", voice_ref=bool(voice), note=note or "")
    res = kf["resolution"]
    inputs = {"prompt": prompt, "image_urls": [kf_url] + urls, "duration": int(b["seconds"]),
              "aspect_ratio": kf["aspect"], "resolution": res, "bitrate_mode": "high", "generate_audio": True}
    if voice:
        inputs["audio_urls"] = [voice]
    return catalog.model_for("ugc_video"), inputs, b["seconds"] * _rate(res)


def broll_inputs(conn, job, p: dict, c: dict) -> tuple:
    """(model, inputs, vendor dollars) of one generated cutaway: image-to-video from the real product photo, silent."""
    import worker
    photos = _photos(conn, job, p)
    if not photos:
        raise hf.GenError("Pick a product photo first.")
    res = broll_resolution(p["kf"]["resolution"])
    inputs = {"prompt": catalog.broll_prompt(_product_name(conn, job, p), c.get("shot") or ""),
              "image_url": worker._remote_url(conn, "photos", photos[0]), "duration": BROLL_SECONDS, "resolution": res,
              "bitrate_mode": "high", "generate_audio": False}
    return catalog.TOOLS["photo_to_video"]["model"], inputs, BROLL_SECONDS * _rate(res)


# ---------------------------------------------------------------- starting a job

def start(conn, job) -> None:
    """A claimed, pending pipeline job: plan it and mark it running. Its parts are sent by advance(). Raises
    hf.GenError when it can't be made at all (the worker fails and refunds it)."""
    import stitch
    p = _params(job)
    if not stitch.have():
        # no ffmpeg on this machine: nothing could be stitched, so nothing is bought
        log.error("job %s: ffmpeg or ffprobe isn't installed here, so a keyframe-first video can't be put together; "
                  "refused before anything was sent to the vendor", job["id"])
        raise hf.GenError(NO_FFMPEG)
    rec = presets.get(p.get("preset"))
    if rec is None or rec.kind != "recipe":
        raise hf.GenError("This video's preset isn't there any more.")
    photos = _photos(conn, job, p)
    if not photos:
        raise hf.GenError("Pick a product photo first.")
    if p.get("fix_of"):
        _start_fix(conn, job, p)
    elif _stale(p):
        # no plan yet, or one made for other settings (a 1080p re-render of a 720p video copies its params, plan
        # and all): planned again from what the job says now
        prod = conn.execute("SELECT * FROM products WHERE id=?", (job["product_id"],)).fetchone() if job["product_id"] else None
        p["kf"] = plan(rec, p, job["brief"] or "", n_photos=len(photos), has_still=has_still(p),
                       category=presets.category(prod), beat_seconds=p.get("beat_seconds"))
    p["kf"]["started_at"] = db.now()
    t = db.now()
    cur = conn.execute("UPDATE jobs SET status='running', request_id=NULL, claimed_at=NULL, error=NULL, params_json=?, "
                       "updated_at=? WHERE id=? AND status='pending'", (json.dumps(p), t, job["id"]))
    conn.commit()
    if cur.rowcount != 1:
        raise hf.GenError("It couldn't start.")


def _stale(p: dict) -> bool:
    """Is the plan a job carries missing, or made for another size, shape or length than the job now has?"""
    kf = p.get("kf")
    if not isinstance(kf, dict) or not kf.get("beats"):
        return True
    if kf.get("resolution") != (p.get("resolution") or catalog.DEFAULT_RESOLUTION) or kf.get("aspect") != (p.get("aspect") or "9:16"):
        return True
    return not p.get("beat_seconds") and int(p.get("duration") or 0) not in (0, int(kf.get("seconds") or 0))


def _start_fix(conn, job, p: dict) -> None:
    """A free fix of one beat: the original's keyframe and its other parts are copied in as done, so only the beat
    the member pointed at is made again."""
    src = conn.execute("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (p["fix_of"], job["workspace_id"])).fetchone()
    if src is None or not isinstance(p.get("kf"), dict):
        raise hf.GenError("The video this fixes isn't there any more.")
    beat = int(p.get("fix_beat", -1))
    folder = parts_dir(job)
    os.makedirs(folder, exist_ok=True)
    t = db.now()
    for (kind, idx), r in _latest(conn, src["id"]).items():
        if kind == "beat" and idx == beat:
            continue
        if r["status"] == "skipped":
            conn.execute("INSERT INTO video_parts (workspace_id, job_id, kind, idx, attempt, status, seconds, usd, "
                         "created_at, updated_at) VALUES (?,?,?,?,1,'skipped',?,0,?,?)",
                         (job["workspace_id"], job["id"], kind, idx, r["seconds"], t, t))
            continue
        if r["status"] != "done" or not r["path"] or not os.path.exists(r["path"]):
            raise hf.GenError("The original's parts are no longer kept, so this one can't be fixed.")
        dst = os.path.join(folder, os.path.basename(r["path"]))
        shutil.copyfile(r["path"], dst)
        conn.execute("INSERT INTO video_parts (workspace_id, job_id, kind, idx, attempt, status, model, seconds, usd, "
                     "path, created_at, updated_at) VALUES (?,?,?,?,1,'done',?,?,0,?,?,?)",
                     (job["workspace_id"], job["id"], kind, idx, r["model"], r["seconds"], dst, t, t))
    conn.commit()


# ---------------------------------------------------------------- one step

def _download(url: str, path: str) -> None:
    import storage
    try:
        storage.disk_guard()
    except storage.DiskFull as e:
        raise hf.GenError("The video couldn't be saved: our server is short on space. Try again later.") from e
    os.makedirs(os.path.dirname(path), exist_ok=True)
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def _price_ok(conn, job, part) -> bool:
    """The vendor's own estimate for this part against what we planned for it (the keyframe and the first beat
    only: the beats of one job are all priced alike). A missing estimate lets it through, as for any video."""
    import worker
    vendor = hf.estimate(part["model"], json.loads(part["inputs_json"] or "{}"))
    try:
        worker.note_estimate(conn, at=db.now(), ok=vendor is not None, job=job["id"], vendor=vendor,
                             ours=round(part["usd"], 4), why=None if vendor is not None else
                             str(hf.last_estimate.get("why") or "no estimate")[:300])
    except Exception:  # noqa: BLE001 — bookkeeping never blocks a video
        log.exception("couldn't record the vendor estimate")
    if vendor is not None and part["usd"] > 0 and vendor > part["usd"] * worker.PRICE_TOLERANCE:
        log.warning("job %s: vendor estimate $%.3f for its %s is over our $%.3f; refused", job["id"], vendor,
                    part["kind"], part["usd"])
        return False
    return True


def _submit(conn, job, part) -> None:
    if part["attempt"] == 1 and not part["tries"] and (part["kind"] == "keyframe" or (part["kind"], part["idx"]) == ("beat", 0)):
        if not _price_ok(conn, job, part):
            raise hf.GenError("This format's price changed and it couldn't start. We've been told.")
    try:
        rid = hf.submit(part["model"], json.loads(part["inputs_json"] or "{}"), idempotency_key=part["submit_key"])
    except hf.Transient as e:
        if hf.retries_on() and part["tries"] + 1 < PART_TRIES:
            _set(conn, part["id"], tries=part["tries"] + 1, error=str(e)[:300])       # the same key again next pass
        else:
            _set(conn, part["id"], status="failed", tries=part["tries"] + 1, error=str(e)[:300])
        return
    except hf.GenError as e:
        _set(conn, part["id"], status="failed", error=str(e)[:300])
        return
    _set(conn, part["id"], status="running", request_id=rid, error=None)


def _poll(conn, job, part) -> bool:
    """Ask the vendor about one running part. True when its state changed."""
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
        outs = hf.outputs(res)
        want = "image" if part["kind"] == "keyframe" else "video"
        o = next((x for x in outs if x["kind"] == want), None)
        if o is None:
            _set(conn, part["id"], status="failed", error="finished with no file")
            return True
        ext = ".png" if want == "image" else ".mp4"
        name = o["url"].rsplit("/", 1)[-1].split("?")[0]
        if want == "image" and "." in name:
            ext = "." + name.rsplit(".", 1)[-1][:5]
        path = os.path.join(parts_dir(job), f"{part['kind']}-{part['idx']}-{part['attempt']}{ext}")
        try:
            _download(o["url"], path)
        except hf.GenError:
            raise
        except Exception as e:  # noqa: BLE001 — a part we can't fetch can't be stitched
            _set(conn, part["id"], status="failed", error=f"couldn't be fetched: {str(e)[:120]}")
            return True
        _set(conn, part["id"], status="made", url=o["url"], path=path)
        return True
    if st == "nsfw":
        _set(conn, part["id"], status="failed", error="content filter: " + (hf.refusal(res) or "no reason given"))
        return True
    if st in hf.FINAL:
        _set(conn, part["id"], status="failed", error="generation failed: " + (hf.refusal(res) or "no reason given"))
        return True
    if old:
        outcome = hf.cancel(part["request_id"])
        log.warning("job %s: its %s %s timed out; vendor cancel: %s", job["id"], part["kind"], part["idx"], outcome)
        _set(conn, part["id"], status="failed", error="Timed out.")
        return True
    return False


def _check(conn, job, p: dict, part) -> None:
    """A downloaded part: the keyframe is always checked, a beat's frames when VIDEO_QA is on, a cutaway never."""
    import image_qa
    if part["kind"] == "keyframe":
        res = image_qa.check_keyframe(conn, job, p, part["attempt"], part["url"] or "", part["path"],
                                      presenter=p["kf"]["keyframe"]["kind"] == "presenter")
    elif part["kind"] == "beat" and video_qa_on():
        k = keyframe_of(conn, job["id"])
        face = k["path"] if (k is not None and p["kf"]["ad_type"] == "ugc" and not p["kf"]["beats"][part["idx"]].get("place")) else None
        res = image_qa.check_beat(conn, job, p, part["idx"], part["attempt"], part["path"], face)
    else:
        res = {"verdict": "pass", "reasons": []}
    if res["verdict"] == "fail":
        _set(conn, part["id"], status="failed", error=("check: " + "; ".join(res["reasons"]))[:300])
    else:
        _set(conn, part["id"], status="done", error=None)


def _step(conn, job, p: dict, part) -> bool:
    """One part one step on: sent, or asked about (and checked in the same pass when it has arrived)."""
    if part["status"] == "pending":
        _submit(conn, job, part)
        return True
    if part["status"] == "running":
        if not _poll(conn, job, part):
            return False
        part = conn.execute("SELECT * FROM video_parts WHERE id=?", (part["id"],)).fetchone()
    if part["status"] == "made":
        _check(conn, job, p, part)
    return True


def _billed_seconds(conn, job_id: int) -> float:
    """Video seconds of this job the vendor has made or is making (a failed or refused request isn't billed)."""
    return sum(r["seconds"] for r in _all(conn, job_id)
               if r["kind"] in ("beat", "broll") and (r["status"] in ("running", "made", "done")
                                                      or (r["status"] == "failed" and (r["error"] or "").startswith("check:"))))


def _fail_text(part) -> str:
    """What the member reads when a part failed for good: the check's own words when the check (or the vendor's
    content filter) turned it down, else the plain failure."""
    import image_qa
    if (part["error"] or "").startswith(("check:", "content filter:")) or part["kind"] == "keyframe":
        return image_qa.FAIL_TEXT
    return "The generation failed."


def advance(conn, job) -> bool:
    """One bounded step of a running pipeline job: send what can be sent, ask about what is being made, check what
    arrived, and when every part is there, stitch and deliver. True when anything changed. Raises hf.GenError to
    end the job (the worker fails and refunds it, and cleanup() drops what was made)."""
    import worker
    p = _params(job)
    kf = p.get("kf")
    if not isinstance(kf, dict):
        raise hf.GenError("This video's plan is missing.")
    if (kf.get("started_at") or job["created_at"]) < worker._minutes_ago(JOB_HOURS * 60):
        raise hf.GenError("Timed out.")
    parts = _latest(conn, job["id"])
    # ---- 1. the keyframe, and its check, before a second of video is paid for
    k = parts.get(("keyframe", 0))
    if k is None:
        model, inputs, usd = keyframe_inputs(conn, job, p)
        pid = _add(conn, job, "keyframe", 0, 1, model, inputs, 0, usd)
        _submit(conn, job, conn.execute("SELECT * FROM video_parts WHERE id=?", (pid,)).fetchone())
        return True
    if k["status"] in ("pending", "running", "made"):
        changed = _step(conn, job, p, k)
        k = conn.execute("SELECT * FROM video_parts WHERE id=?", (k["id"],)).fetchone()
        if k["status"] not in ("done", "failed"):
            return changed
    if k["status"] == "failed":
        if k["attempt"] >= 2:
            import image_qa
            raise hf.GenError(_fix_text(p) or image_qa.FAIL_TEXT)
        model, inputs, usd = keyframe_inputs(conn, job, p)
        log.info("job %s: its keyframe wasn't right (%s); making it again once", job["id"], (k["error"] or "")[:120])
        pid = _add(conn, job, "keyframe", 0, 2, model, inputs, 0, usd)
        _submit(conn, job, conn.execute("SELECT * FROM video_parts WHERE id=?", (pid,)).fetchone())
        return True
    # ---- 2. every beat and every generated cutaway, at once
    want = [("beat", b["i"], None) for b in kf["beats"]] + \
           [("broll", c["beat"], c) for c in kf["cutaways"] if c["kind"] == "video"]
    kf_url = None
    changed, waiting = False, False
    for kind, i, c in want:
        part = parts.get((kind, i))
        if part is None or (part["status"] == "failed" and _again(conn, job, p, part)):
            attempt = 1 if part is None else part["attempt"] + 1
            if kind == "beat":
                kf_url = kf_url or _keyframe_url(conn, k)
                model, inputs, usd = beat_inputs(conn, job, p, i, kf_url)
                secs = kf["beats"][i]["seconds"]
            else:
                model, inputs, usd = broll_inputs(conn, job, p, c)
                secs = BROLL_SECONDS
            pid = _add(conn, job, kind, i, attempt, model, inputs, secs, usd)
            _submit(conn, job, conn.execute("SELECT * FROM video_parts WHERE id=?", (pid,)).fetchone())
            changed = waiting = True
            continue
        if part["status"] in ("pending", "running", "made"):
            changed = _step(conn, job, p, part) or changed
            part = conn.execute("SELECT * FROM video_parts WHERE id=?", (part["id"],)).fetchone()
        if part["status"] == "failed":
            if _again(conn, job, p, part):
                waiting = changed = True            # its second try is sent on the next pass
                continue
            if kind == "broll":                     # a cutaway that can't be made: the packshot itself stands in
                _set(conn, part["id"], status="skipped")
                changed = True
                continue
            raise hf.GenError(_fix_text(p) or _fail_text(part))
        if part["status"] not in ("done", "skipped"):
            waiting = True
    if waiting:
        return changed
    # ---- 3. everything is there
    _stitch(conn, job, p)
    return True


def _again(conn, job, p: dict, part) -> bool:
    """May this failed part have its one more try? Not a second time, and not past the job's seconds cap."""
    if part["attempt"] >= 2:
        return False
    kf = p["kf"]
    return _billed_seconds(conn, job["id"]) + part["seconds"] <= hard_seconds(kf["seconds"], kf.get("resolution") or "") + 0.01


def _fix_text(p: dict) -> str:
    if p.get("free_fix"):
        import free_fix
        return free_fix.FAIL_TEXT
    return ""


# ---------------------------------------------------------------- putting it together

def _stitch(conn, job, p: dict) -> None:
    import stitch
    import textlayer
    import videotext
    import worker
    kf = p["kf"]
    parts = _latest(conn, job["id"])
    folder = parts_dir(job)
    work = os.path.join(folder, "work")
    os.makedirs(work, exist_ok=True)
    photos = _photos(conn, job, p)
    packshot = next((ph["path"] for ph in photos if ph["path"] and os.path.exists(ph["path"])), None)
    brand = conn.execute("SELECT * FROM brands WHERE id=?", (job["brand_id"],)).fetchone() if job["brand_id"] else None
    prod = conn.execute("SELECT * FROM products WHERE id=?", (job["product_id"],)).fetchone() if job["product_id"] else None
    kit = textlayer.kit_of(brand)
    aspect = kf["aspect"]
    safe_bottom = textlayer.SAFE.get(aspect, textlayer.SAFE["9:16"])[3]
    try:
        first = parts[("beat", 0)]["path"]
        size, fps = stitch.standard(first)
        clips = []
        for b in kf["beats"]:
            i, secs = b["i"], float(b["seconds"])
            overlays, n = [], 0
            for lab in kf.get("labels") or []:
                path = os.path.join(work, f"label-{i}-{n}.png")
                videotext.tag_png(lab, size, path, kit, aspect, corner="right", accent=False)
                overlays.append({"path": path, "start": 0, "end": secs})
                n += 1
            if b.get("tag"):
                path = os.path.join(work, f"tag-{i}.png")
                videotext.tag_png(b["tag"], size, path, kit, aspect, corner="left")
                overlays.append({"path": path, "start": 0, "end": secs})
            screen = screen_for(conn, job, p, photos, b) if b.get("screen") else None
            if kf.get("captions"):
                # over a full-frame screen the presenter's small picture sits low, so the words go high; with the
                # screen across the top (split) they stay where they always are, over the presenter
                high = screen is not None and stitch.screen_layout(screen, size) == "pip"
                for j, ch in enumerate(videotext.caption_chunks(b["line"], secs)):
                    path = os.path.join(work, f"cap-{i}-{j}.png")
                    videotext.caption_png(ch["text"], size, path, kit, aspect, high=high)
                    overlays.append({"path": path, "start": ch["start"], "end": ch["end"]})
            cuts = []
            for c in kf["cutaways"]:
                if c["beat"] != i:
                    continue
                made = parts.get(("broll", i))
                if c["kind"] == "video" and made is not None and made["status"] == "done" and made["path"]:
                    cuts.append({"path": made["path"], "start": c["start"], "seconds": c["seconds"], "kind": "video"})
                elif packshot:
                    cuts.append({"path": packshot, "start": c["start"], "seconds": c["seconds"], "kind": "still"})
            dst = os.path.join(work, f"beat-{i}.mp4")
            stitch.render_beat(parts[("beat", i)]["path"], dst, size=size, fps=fps, seconds=secs, cutaways=cuts,
                               screen=screen, overlays=overlays, safe_bottom=safe_bottom)
            clips.append(dst)
        if kf.get("end_card"):
            card = os.path.join(work, "end-card.png")
            badge = ""
            if kf["end_card"].get("badge") and prod is not None:
                badge = (prod["offer"] or prod["price"] or "").strip()      # only what is saved on the product
            videotext.end_card(size, card, packshot=packshot, kit=kit, aspect=aspect,
                               headline=(prod["name"] if prod is not None else p.get("product") or ""),
                               subline="" if kit.get("logo") else (kit.get("brand") or ""), badge=badge,
                               cta=" ".join(str(p.get("cta") or "").split())[:30] or videotext.cta_for(kf.get("category") or ""))
            end = os.path.join(work, "end-card.mp4")
            stitch.still_clip(card, end, size=size, fps=fps, seconds=float(kf["end_card"]["seconds"]))
            clips.append(end)
        out_dir = os.path.join(worker.MEDIA, str(job["workspace_id"]), str(job["id"]))
        final = os.path.join(out_dir, f"campaignify-{job['id']}-1.mp4")
        info = stitch.concat(clips, final, fps=fps, crossfade_frames=int(kf.get("crossfade") or 0),
                             tail_fade=0.25 if kf.get("end_card") else 0.0)
    except stitch.StitchError as e:
        log.warning("job %s: stitching failed: %s", job["id"], str(e)[:300])
        raise hf.GenError(_fix_text(p) or STITCH_FAIL) from e
    if not worker._still_running(conn, job):         # canceled meanwhile (refunded): nothing is kept
        cleanup(conn, job)
        return
    worker.make_poster(final)
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (job["id"], "video", "", final))
    conn.commit()
    if not worker._finish_done(conn, job):
        worker._drop_outputs(conn, job)
        return
    log.info("job %s: stitched %s beat(s) into %.1f s", job["id"], len(kf["beats"]), info["duration"])
    shutil.rmtree(work, ignore_errors=True)
    tidy(conn, job)
    if p.get("fix_of"):          # the original's one fix is used: its kept beats aren't needed any more
        src = conn.execute("SELECT * FROM jobs WHERE id=?", (p["fix_of"],)).fetchone()
        if src is not None:
            tidy(conn, src, keep_beats=False)


def tidy(conn, job, keep_beats: bool | None = None) -> None:
    """After delivery: the keyframe is kept (it is the job's approved still); the beats and cutaways are kept only
    while a free fix could need them (FREE_FIX on, and not a fix itself; `keep_beats` says otherwise), else removed."""
    import free_fix
    p = _params(job)
    keep = (free_fix.on() and not p.get("free_fix")) if keep_beats is None else keep_beats
    for r in _all(conn, job["id"]):
        if r["kind"] == "keyframe" and r["status"] == "done":
            continue
        if keep and r["status"] == "done":
            continue
        if r["path"] and os.path.exists(r["path"]):
            try:
                os.remove(r["path"])
            except OSError:
                pass
            conn.execute("UPDATE video_parts SET path=NULL WHERE id=?", (r["id"],))
    conn.commit()


def cleanup(conn, job) -> None:
    """The job ended without a video (failed, refunded, canceled): ask the vendor to drop what is still being made
    and remove what was fetched. Never raises."""
    try:
        for r in _all(conn, job["id"]):
            if r["status"] == "running" and r["request_id"]:
                try:
                    hf.cancel(r["request_id"])
                except Exception:  # noqa: BLE001
                    pass
            if r["status"] in ("pending", "running", "made"):
                conn.execute("UPDATE video_parts SET status='failed', error=COALESCE(error, 'the job ended'), updated_at=? "
                             "WHERE id=?", (db.now(), r["id"]))
        conn.execute("UPDATE video_parts SET path=NULL WHERE job_id=?", (job["id"],))
        conn.commit()
        shutil.rmtree(parts_dir(job), ignore_errors=True)
    except Exception:  # noqa: BLE001 — bookkeeping never undoes a refund
        log.exception("job %s: cleanup of its video parts didn't finish", job["id"])


def can_cancel(conn, job) -> bool:
    """A member may cancel while only the keyframe is being made; once beats are at the vendor they are paid for."""
    return not any(r["kind"] in ("beat", "broll") for r in _all(conn, job["id"]))


def fix_ready(conn, job) -> bool:
    """Are this delivered video's keyframe and beats still on disk (what a one-beat fix starts from)?"""
    p = _params(job)
    kf = p.get("kf")
    if not isinstance(kf, dict):
        return False
    latest = _latest(conn, job["id"])
    need = [("keyframe", 0)] + [("beat", b["i"]) for b in kf["beats"]]
    return all(k in latest and latest[k]["status"] == "done" and latest[k]["path"] and os.path.exists(latest[k]["path"])
               for k in need)


_purged = {"day": ""}


def maybe_purge() -> int:
    """Once a day: the kept beats of videos delivered more than KEEP_DAYS ago are removed (their free fix has
    lapsed). Never raises; returns how many jobs were tidied."""
    import worker
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _purged["day"] == day:
        return 0
    _purged["day"] = day
    n = 0
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT j.* FROM jobs j JOIN video_parts v ON v.job_id=j.id WHERE j.status='done' AND "
                "v.kind IN ('beat','broll') AND v.path IS NOT NULL AND j.finished_at < ? LIMIT 200",
                (worker._minutes_ago(KEEP_DAYS * 24 * 60),)).fetchall()
            for j in rows:
                tidy(conn, j, keep_beats=False)          # only the keyframe stays
                n += 1
    except Exception:  # noqa: BLE001
        log.exception("purging old video parts didn't finish")
    return n

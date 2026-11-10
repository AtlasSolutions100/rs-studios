"""Automatic QA of a finished picture against the product photo (quality wave 2). Switch: IMAGE_QA, default off.

One part of it is not behind the switch (m15): the text-free picture of a static with a text layer is ALWAYS checked
for words the image model painted in by itself and for a person in it (LAYERED_CHECKS), before any of our own layers
is drawn; a failure is made again once, silently, and refunded when it fails twice. Invented copy in a picture is a
compliance problem, so it doesn't wait for a member-facing switch. That costs us one check ($0.004) a layered picture.

One vision call per picture (llm.structured, the fast model unless IMAGE_QA_MODEL names another) is shown the
member's product photo and the finished picture and answers plain questions. The verdict is then worked out
HERE from those answers, never taken from the model as a yes or no:

    product_present            the product is in the picture
    label_matches              its label and logo match the photo, and can be read where the photo's can
    no_extra_text              no words besides the ones this ad is allowed (its headline, subline, button and
                               brand name; none at all for a picture that gets its words from textlayer.py)
                               and what is printed on the product itself; nothing garbled
    no_deformed_hands_faces    no malformed hands or faces
    no_people                  (statics with a text layer that show the product alone, presets.product_only)
                               nobody is in the picture: a person there is where the words go
    (keyframe only) one clear presenter face, the product not covering it

What the worker does with the verdict (worker._finish_images): a picture that fails is never shown. The first
time, the failed ones are made again once, silently (the member only sees "Making it"). One that fails twice is
dropped and its credits come straight back; when none is left the job ends with FAIL_TEXT. Every picture looked
at gets an asset_qa row (pass / fail / skipped, the reasons, the model's answers, what the check cost), which
is what the acceptance numbers are counted from (acceptance()).

The check costs us, not the member: about $0.004 a picture on the fast model (two images, about 2,600 tokens in
and 250 out; cost() shows the estimate for the model in use). A silent retry adds one more generation
($0.1071 a picture) and one more check. IMAGE_QA_DAILY_MAX (default 2000 checks a day) caps the spend; past it,
and whenever the check itself can't run (no key, the model is down, no product photo), pictures are delivered
unchecked and the row says "skipped": QA never holds an ad hostage.

A keyframe-first video (keyframe_video.py, KEYFRAME_VIDEO) is checked here too. Its keyframe is checked with
kind="keyframe" before any video is paid for, whatever IMAGE_QA says (check_keyframe). With VIDEO_QA on (default
off), three frames of every beat are checked against the product photo and the keyframe's face (check_beat). Those
checks have their own daily budget, VIDEO_QA_DAILY_MAX (default 3000, about $15 at the fast model's price), counted
apart from the pictures' (asset_qa.kind "keyframe" / "frame").
"""
import json
import logging
import os

import llm

log = logging.getLogger("campaignify")
FAIL_TEXT = "We couldn't get this one right — credits returned."
# What the text-free picture of a static with a text layer is always checked for, with or without IMAGE_QA (the
# worker runs the check before any layer is drawn, so our own words are never what it reads): words the image model
# painted in by itself, which have been invented claims, and a person standing where the words go. The other
# checks (product, label, hands) stay the member-facing switch's.
LAYERED_CHECKS = ("no_extra_text", "no_people")
DEFAULT_DAILY_MAX = 2000
TOKENS = (2600, 250)            # per check: two images and the questions in, the answers out
CHECKS = {
    "product_present": "The product from the photo is in the picture.",
    "label_matches": "Its label and logo match the photo and can be read.",
    "no_extra_text": "No words other than the ad's own and the product's packaging; nothing garbled.",
    "no_deformed_hands_faces": "No malformed hands or faces.",
    "keyframe": "A presenter keyframe: one clear face, the product not covering it (checked before any video).",
}
VIDEO_CHECKS = {"five_tells": "Waxy skin, wrong hands, lighting mismatch, face drift, lip-sync drift (not automated yet).",
                "lip_sync": "Lips match the speech (not automated yet)."}
SYSTEM = ("You inspect AI-generated product ad images for an advertiser. You are shown the seller's own product photo "
          "first, then the generated image. Answer each question about the generated image exactly and literally. "
          "Do not judge style or taste. When you can't tell, say so with the stricter answer.")
FACE_QUESTION = {"type": "boolean", "description": "The person in the generated image is the same person as in the "
                 "keyframe image: the same face, hair and age (true when no person is visible in the generated image)."}
SCHEMA = {"type": "object", "properties": {
    "product_present": {"type": "boolean", "description": "The same product as in the photo is visible."},
    "photo_has_label": {"type": "boolean", "description": "The product in the PHOTO carries readable text or a logo."},
    "label_matches": {"type": "boolean", "description": "The label, logo, printed text and colours on the product "
                      "match the photo (true when the photo has no label)."},
    "label_legible": {"type": "boolean", "description": "The label text on the product can be read as in the photo, "
                      "not smeared or melted (true when the photo has no label)."},
    "other_text": {"type": "array", "items": {"type": "string"}, "description": "Every piece of text in the image "
                   "that is NOT printed on the product itself, word for word, one entry per piece. Empty if none."},
    "garbled_text": {"type": "boolean", "description": "Any text anywhere that is misspelt, melted or nonsense."},
    "people": {"type": "boolean", "description": "Hands, faces or people are visible."},
    "deformed_hands": {"type": "boolean", "description": "A visible hand has the wrong number of fingers or is malformed."},
    "deformed_faces": {"type": "boolean", "description": "A visible face is distorted or malformed."},
    "one_clear_face": {"type": "boolean", "description": "Exactly one person's face is clearly visible and unobstructed."},
    "notes": {"type": "string", "description": "One short sentence on the worst problem, or empty."}},
    "required": ["product_present", "label_matches", "label_legible", "other_text", "garbled_text", "people",
                 "deformed_hands", "deformed_faces"]}


def on() -> bool:
    return os.environ.get("IMAGE_QA", "0").strip().lower() in ("1", "on", "true", "yes")


def model_id() -> str:
    return (os.environ.get("IMAGE_QA_MODEL") or "").strip() or llm.fast_model()


def daily_max() -> int:
    try:
        return int(os.environ.get("IMAGE_QA_DAILY_MAX", str(DEFAULT_DAILY_MAX)))
    except ValueError:
        return DEFAULT_DAILY_MAX


def cost() -> dict:
    """What the switch costs us, for the admin page and the docs (estimates from the price tables)."""
    import catalog
    check = llm.cost_usd(model_id(), *TOKENS)
    image = catalog.TOOLS["ad_image"]["usd_each"]
    return {"check_usd": round(check, 4), "retry_usd": round(image + check, 4), "image_usd": image,
            "model": model_id(), "daily_max": daily_max(), "daily_max_usd": round(check * daily_max(), 2)}


def _norm(s: str) -> str:
    import re
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def extra_text(found, allowed) -> list:
    """The pieces of text the model saw that this ad isn't allowed: anything that isn't one of the allowed
    strings (or made only of their words)."""
    ok = [_norm(a) for a in allowed if _norm(a)]
    words = set(" ".join(ok).split())
    out = []
    for t in found or []:
        n = _norm(str(t))
        if not n or n in ok or any(n in a for a in ok) or all(w in words for w in n.split()):
            continue
        out.append(" ".join(str(t).split())[:80])
    return out


def judge(ans: dict, allowed=(), kind: str = "image", no_people: bool = False, only=None, face: bool = False) -> dict:
    """{"pass", "reasons", "checks"} from the model's answers. A missing answer counts as the strict one.
    `no_people`: the picture is meant to show the product alone (a static with a text layer), so one the model
    says has people in it fails (only on a plain yes: this answer isn't taken as strict when it is missing).
    `only`: the checks that decide (LAYERED_CHECKS when IMAGE_QA is off); the others are still recorded in
    "checks" but neither fail the picture nor give a reason."""
    ans = ans if isinstance(ans, dict) else {}
    extra = extra_text(ans.get("other_text"), allowed)
    has_label = ans.get("photo_has_label", True) is not False
    checks = {"product_present": ans.get("product_present") is True,
              "label_matches": ans.get("label_matches") is True and (not has_label or ans.get("label_legible") is True),
              "no_extra_text": not extra and ans.get("garbled_text") is False,
              "no_deformed_hands_faces": ans.get("deformed_hands") is False and ans.get("deformed_faces") is False}
    why = {}                                    # check -> its reason, in the order they are told
    if not checks["product_present"]:
        why["product_present"] = "The product isn't in the picture."
    elif not checks["label_matches"]:
        why["label_matches"] = ("The label or logo doesn't match the product photo." if ans.get("label_matches") is not True
                                else "The label can't be read.")
    if extra:
        why["no_extra_text"] = "Extra text: " + "; ".join(extra[:4])
    elif not checks["no_extra_text"]:
        why["no_extra_text"] = "Garbled text."
    if not checks["no_deformed_hands_faces"]:
        why["no_deformed_hands_faces"] = "A hand or a face is malformed."
    if kind == "keyframe":
        checks["keyframe"] = ans.get("one_clear_face") is True
        if not checks["keyframe"]:
            why["keyframe"] = "The presenter's face isn't clear."
    if no_people:
        checks["no_people"] = ans.get("people") is not True
        if not checks["no_people"]:
            why["no_people"] = "A person is in a picture that is meant to show the product alone."
    if face:                    # a video frame against its keyframe: the presenter must still be that person
        checks["same_person"] = ans.get("same_person") is not False
        if not checks["same_person"]:
            why["same_person"] = "The presenter's face changed from the keyframe."
    reasons = [r for k, r in why.items() if only is None or k in only]
    return {"pass": not reasons, "reasons": reasons, "checks": checks}


def _small(src, side: int):
    """(JPEG bytes, media type) of a picture (path or bytes), at most `side` px, or None."""
    try:
        import io
        import imagesafe
        im = imagesafe.thumbnail(src, (side, side), mode="RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88)
        return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        return None


def inspect(image, product_photo, allowed_text=(), kind: str = "image", product: str = "",
            usage: llm.Usage | None = None, no_people: bool = False, only=None, face=None) -> dict:
    """Look at one picture. `image` and `product_photo` are file paths or bytes. Returns judge()'s dict plus
    {"answers", "model", "usd"}. Raises llm.LLMError when the check can't run (the caller delivers unchecked).
    `face`: a video's keyframe (path or bytes): the picture is a frame of that video, and the model is also asked
    whether its presenter is still the keyframe's person."""
    usage = usage if usage is not None else llm.Usage()
    pic, ref = _small(image, 1024), _small(product_photo, 768)
    if pic is None or ref is None:
        raise llm.LLMError("a picture couldn't be read")
    key = _small(face, 768) if face is not None else None
    if key is not None:
        return _inspect_frame(pic, ref, key, product, usage, only)
    allowed = [a for a in allowed_text if a]
    what = {"keyframe": "presenter keyframe", "frame": "frame of a video ad"}.get(kind, "ad image")
    prompt = (f"Image 1 is the seller's photo of the product ({product or 'the product'}). Image 2 is the generated "
              f"{what}. Answer about image 2.\n"
              + ("Text this ad is meant to carry (do not list it under other_text unless it is misspelt): "
                 + "; ".join(f'"{a}"' for a in allowed) if allowed else
                 "This image is meant to carry no text at all apart from what is printed on the product.")
              + "\nList under other_text every other piece of text you can see that is not printed on the product.")
    before = usage.usd
    ans = llm.structured(system=SYSTEM, prompt=prompt, schema=SCHEMA, name="report", max_tokens=TOKENS[1] + 150,
                         usage=usage, temperature=0, model_id=model_id(), images=[ref, pic])
    out = judge(ans, allowed, kind, no_people, only)
    out.update(answers=ans, model=model_id(), usd=round(usage.usd - before, 6))
    return out


def _inspect_frame(pic, ref, key, product: str, usage, only) -> dict:
    """One frame of a video against the product photo AND the video's keyframe (three pictures)."""
    schema = dict(SCHEMA, properties=dict(SCHEMA["properties"], same_person=FACE_QUESTION),
                  required=SCHEMA["required"] + ["same_person"])
    prompt = (f"Image 1 is the seller's photo of the product ({product or 'the product'}). Image 2 is the approved "
              "keyframe of a video ad: its presenter and its scene. Image 3 is a frame taken from the generated "
              "video. Answer about image 3.\nThis frame is meant to carry no text at all apart from what is printed on "
              "the product.\nList under other_text every other piece of text you can see that is not printed on the "
              "product.")
    before = usage.usd
    ans = llm.structured(system=SYSTEM, prompt=prompt, schema=schema, name="report", max_tokens=TOKENS[1] + 150,
                         usage=usage, temperature=0, model_id=model_id(), images=[ref, key, pic])
    out = judge(ans, (), "frame", only=only, face=True)
    out.update(answers=ans, model=model_id(), usd=round(usage.usd - before, 6))
    return out


# ---------------------------------------------------------------- a keyframe-first video's checks (keyframe_video.py)

def video_daily_max() -> int:
    import keyframe_video
    return keyframe_video.video_qa_daily_max()


def _skip(conn, job, p, attempt, idx, why, url, path, kind) -> dict:
    return {"verdict": "skipped", "reasons": [why],
            "row": record(conn, job, p, attempt, idx, "skipped", [why], url=url, path=path, kind=kind)}


def _why_not(conn, path, p) -> str:
    if not path or not os.path.exists(path):
        return "no local copy to look at"
    if not llm.anthropic_ready():
        return "the vision model isn't set up"
    if checks_today(conn, VIDEO_KINDS) >= video_daily_max():
        return "today's check budget is used"
    return ""


def check_keyframe(conn, job, p: dict, attempt: int, url: str, path: str | None, presenter: bool = True) -> dict:
    """The check of a video's keyframe, run whatever IMAGE_QA says (a keyframe is the cheapest place to stop a bad
    video): the product is there, its label is right, nothing else is written anywhere, hands and face are fine,
    and for a presenter's keyframe there is one clear face the product doesn't cover. Writes an asset_qa row
    (kind "keyframe"). {"verdict": pass | fail | skipped, "reasons", "row"}; never raises, and a check that can't
    run counts as skipped (the video goes ahead)."""
    why = _why_not(conn, path, p)
    ref = None if why else product_photo(conn, job, p)
    if not why and ref is None:
        why = "no product photo to compare with"
    if why:
        return _skip(conn, job, p, attempt, 1, why, url, path, "keyframe")
    try:
        res = inspect(path, ref, (), kind="keyframe" if presenter else "image", product=p.get("product") or "")
    except Exception as e:  # noqa: BLE001 — a check never holds a video hostage
        log.warning("keyframe check for job %s could not run: %s", job["id"], str(e)[:160])
        return _skip(conn, job, p, attempt, 1, "the check couldn't run", url, path, "keyframe")
    verdict = "pass" if res["pass"] else "fail"
    return {"verdict": verdict, "reasons": res["reasons"],
            "row": record(conn, job, p, attempt, 1, verdict, res["reasons"], res["checks"], res["model"], res["usd"],
                          url, path, kind="keyframe")}


FRAME_FAILS = 2          # a beat fails when this many of its sampled frames do (one odd mid-motion frame doesn't)


def check_beat(conn, job, p: dict, beat: int, attempt: int, clip: str | None, face=None) -> dict:
    """The frame check of one beat (VIDEO_QA): three frames out of the clip, each against the product photo and,
    for a presenter beat, the keyframe's face. One asset_qa row a frame (kind "frame", idx = beat * 10 + frame).
    The beat fails when FRAME_FAILS of its frames fail. Never raises; what can't be checked counts as skipped."""
    import stitch
    why = _why_not(conn, clip, p)
    ref = None if why else product_photo(conn, job, p)
    if not why and ref is None:
        why = "no product photo to compare with"
    if why:
        return _skip(conn, job, p, attempt, beat * 10, why, "", clip, "frame")
    folder = os.path.join(os.path.dirname(clip), "work", f"frames-{beat}-{attempt}")
    shots = stitch.frames(clip, folder, 3)
    if not shots:
        return _skip(conn, job, p, attempt, beat * 10, "no frame could be cut", "", clip, "frame")
    bad, reasons, rows = 0, [], []
    for n, (_t, shot) in enumerate(shots, start=1):
        if checks_today(conn, VIDEO_KINDS) >= video_daily_max():
            rows.append(record(conn, job, p, attempt, beat * 10 + n, "skipped", ["today's check budget is used"],
                               path=shot, kind="frame"))
            continue
        try:
            res = inspect(shot, ref, (), kind="frame", product=p.get("product") or "", face=face)
        except Exception as e:  # noqa: BLE001
            log.warning("frame check for job %s beat %s could not run: %s", job["id"], beat, str(e)[:160])
            rows.append(record(conn, job, p, attempt, beat * 10 + n, "skipped", ["the check couldn't run"], path=shot,
                               kind="frame"))
            continue
        verdict = "pass" if res["pass"] else "fail"
        if verdict == "fail":
            bad += 1
            reasons += [r for r in res["reasons"] if r not in reasons]
        rows.append(record(conn, job, p, attempt, beat * 10 + n, verdict, res["reasons"], res["checks"], res["model"],
                           res["usd"], "", shot, kind="frame"))
    conn.commit()
    failed = bad >= min(FRAME_FAILS, len(shots))
    return {"verdict": "fail" if failed else "pass", "reasons": reasons if failed else [], "rows": rows, "bad": bad}


# ---------------------------------------------------------------- per job (the worker)

VIDEO_KINDS = ("keyframe", "frame")         # a keyframe-first video's checks: their own daily budget (keyframe_video.py)


def checks_today(conn, kinds=("image",)) -> int:
    """Checks run today (UTC) of these kinds. A picture's check and a video's keyframe and frame checks are counted
    apart, so neither budget (IMAGE_QA_DAILY_MAX, VIDEO_QA_DAILY_MAX) eats the other's."""
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    marks = ",".join("?" * len(kinds))
    return conn.execute(f"SELECT COUNT(*) FROM asset_qa WHERE created_at >= ? AND verdict != 'skipped' AND kind IN ({marks})",
                        (day, *kinds)).fetchone()[0]


def allowed_text(conn, job, p: dict) -> list:
    """The words a picture of this job may carry. A layered static gets its words afterwards, so none."""
    if p.get("layout"):
        return []
    import catalog
    parts = catalog.split_image_brief(job["brief"] or "")
    brand = conn.execute("SELECT brand_name FROM brands WHERE id=?", (job["brand_id"],)).fetchone() \
        if job["brand_id"] else None
    return [x for x in (p.get("headline") or parts["headline"], p.get("subline") or parts["subline"],
                        p.get("cta") or parts["cta"], brand["brand_name"] if brand else "") if x]


def product_photo(conn, job, p: dict):
    for pid in p.get("photo_ids") or []:
        row = conn.execute("SELECT path FROM photos WHERE id=? AND workspace_id=?", (pid, job["workspace_id"])).fetchone()
        if row and row["path"] and os.path.exists(row["path"]):
            return row["path"]
    return None


def record(conn, job, p: dict, attempt: int, idx: int, verdict: str, reasons=(), checks=None, model: str = "",
           usd: float = 0.0, url: str = "", path: str | None = None, kind: str = "image") -> int:
    import catalog
    import db
    return conn.execute(
        "INSERT INTO asset_qa (workspace_id, job_id, attempt, idx, kind, ad_type, preset, verdict, reasons_json, "
        "checks_json, model, usd, url, path, kept, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
        (job["workspace_id"], job["id"], attempt, idx, kind, catalog.job_ad_type(job["tool"], p),
         p.get("preset") or job["template_id"] or "", verdict, json.dumps(list(reasons)), json.dumps(checks or {}),
         model, usd, url, path, db.now())).lastrowid


def check_output(conn, job, p: dict, attempt: int, idx: int, url: str, path: str | None, only=None) -> dict:
    """Check one finished picture of a job and write its asset_qa row. {"verdict": pass | fail | skipped,
    "reasons", "row"}. Never raises: when the check can't run the picture counts as skipped (delivered).
    `only`: the checks that may fail it (see judge)."""
    why = ""
    if not path or not os.path.exists(path):
        why = "no local copy to look at"
    elif p.get("service"):
        why = "a service ad (no product packaging to compare)"
    elif not llm.anthropic_ready():
        why = "the vision model isn't set up"
    elif checks_today(conn, ("image",)) >= daily_max():
        why = "today's check budget is used"
    ref = None if why else product_photo(conn, job, p)
    if not why and ref is None:
        why = "no product photo to compare with"
    if why:
        return {"verdict": "skipped", "reasons": [why],
                "row": record(conn, job, p, attempt, idx, "skipped", [why], url=url, path=path)}
    try:
        import presets
        res = inspect(path, ref, allowed_text(conn, job, p), product=p.get("product") or "",
                      no_people=bool(p.get("layout")) and presets.product_only(p.get("preset")), only=only)
    except Exception as e:  # noqa: BLE001 — QA never holds an ad hostage
        log.warning("image check for job %s could not run: %s", job["id"], str(e)[:160])
        why = "the check couldn't run"
        return {"verdict": "skipped", "reasons": [why],
                "row": record(conn, job, p, attempt, idx, "skipped", [why], url=url, path=path)}
    verdict = "pass" if res["pass"] else "fail"
    return {"verdict": verdict, "reasons": res["reasons"],
            "row": record(conn, job, p, attempt, idx, verdict, res["reasons"], res["checks"], res["model"],
                          res["usd"], url, path)}


def kept(conn, job_id: int) -> list:
    """Pictures of an earlier try that passed and wait for the retry of the rest."""
    return conn.execute("SELECT * FROM asset_qa WHERE job_id=? AND kept=1 ORDER BY id", (job_id,)).fetchall()


def discard(conn, job_id: int) -> int:
    """A job ended without delivering: remove the passed pictures it was holding back."""
    n = 0
    for r in kept(conn, job_id):
        for f in (r["path"],):
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                    n += 1
                except OSError:
                    pass
    conn.execute("UPDATE asset_qa SET kept=0 WHERE job_id=? AND kept=1", (job_id,))
    return n


def acceptance(conn, days: int = 30) -> list:
    """Per ad type and preset over the last `days`: pictures checked, passed first time, passed on the retry,
    failed twice (refunded), skipped, and what the checks cost. The first-render QA rate per preset."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    rows = conn.execute(
        "SELECT ad_type, preset, SUM(verdict!='skipped') checked, SUM(verdict='pass' AND attempt=1) first_pass, "
        "SUM(verdict='pass' AND attempt>1) retry_pass, SUM(verdict='fail' AND attempt=1) first_fail, "
        "SUM(verdict='fail' AND attempt>1) refunded, SUM(verdict='skipped') skipped, SUM(usd) usd "
        "FROM asset_qa WHERE created_at >= ? GROUP BY ad_type, preset ORDER BY checked DESC", (since,)).fetchall()
    out = []
    for r in rows:
        first = (r["first_pass"] or 0) + (r["first_fail"] or 0)
        out.append({"ad_type": r["ad_type"] or "", "preset": r["preset"] or "", "checked": r["checked"] or 0,
                    "first_pass": r["first_pass"] or 0, "retry_pass": r["retry_pass"] or 0,
                    "first_fail": r["first_fail"] or 0, "refunded": r["refunded"] or 0, "skipped": r["skipped"] or 0,
                    "usd": round(r["usd"] or 0, 4),
                    "first_pass_rate": round((r["first_pass"] or 0) / first, 3) if first else None})
    return out


def reasons_for(conn, job_id: int) -> list:
    """Why pictures of this job failed, newest first (the admin's view; members only see FAIL_TEXT)."""
    out = []
    for r in conn.execute("SELECT attempt, idx, reasons_json FROM asset_qa WHERE job_id=? AND verdict='fail' ORDER BY id DESC",
                          (job_id,)):
        try:
            out.append({"attempt": r["attempt"], "idx": r["idx"], "reasons": json.loads(r["reasons_json"] or "[]")})
        except ValueError:
            pass
    return out

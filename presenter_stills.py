"""One sample still per AI presenter, shown on the presenter cards (owner decision D12).

The presenters are described, never real people: each still is an AI-generated
image from the presenter's description, made once through the vendor's
text-to-image model and kept. Spending is capped (RS_PRESENTER_STILLS_CAP_USD,
default $3) and tracked in meta 'presenter_stills'; each still is priced with the
vendor's free estimate before it is started, and a failed one is not counted
(the vendor doesn't charge for failures).

Files: static/presenters/<id>.jpg when committed to the repo, else
<data>/presenters/<id>.jpg (made from /admin, survives deploys).
"""
import io
import json
import os

import catalog
import db
import hf

MODEL = os.environ.get("RS_PRESENTER_STILL_MODEL", "higgsfield-ai/soul/standard")
CAP_USD = float(os.environ.get("RS_PRESENTER_STILLS_CAP_USD", "3") or 3)
FALLBACK_USD = float(os.environ.get("RS_PRESENTER_STILL_USD", "0.25") or 0.25)   # when the vendor gives no estimate
DATA_DIR = os.path.join(db.DATA, "presenters")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "presenters")
KEY = "presenter_stills"


def path_for(pid: str) -> str | None:
    for d in (STATIC_DIR, DATA_DIR):
        p = os.path.join(d, f"{pid}.jpg")
        if os.path.exists(p):
            return p
    return None


def photo_url(pid: str, url_for) -> str | None:
    if os.path.exists(os.path.join(STATIC_DIR, f"{pid}.jpg")):
        return url_for("static", filename=f"presenters/{pid}.jpg")
    if os.path.exists(os.path.join(DATA_DIR, f"{pid}.jpg")):
        return url_for("presenter_photo", pid=pid)
    return None


def with_photos(url_for) -> list[dict]:
    return [dict(p, photo=photo_url(p["id"], url_for)) for p in catalog.PRESENTERS]


def inputs(pr: dict) -> dict:
    return {"prompt": (f"Photorealistic head-and-shoulders portrait of a fictional, AI-generated person (not a real or "
                       f"famous person): a {pr['look']}, in {pr['setting']}. Friendly, natural expression, looking at "
                       "the camera like a phone selfie video, soft natural light, shallow depth of field. No text, no "
                       "logos, no watermark."),
            "num_images": 1, "resolution": "2K", "aspect_ratio": "3:4"}


def state(conn) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    st = json.loads(row["value"]) if row else {}
    st.setdefault("spent", 0.0)
    st.setdefault("jobs", {})
    return st


def _save_state(conn, st):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (KEY, json.dumps(st)))
    conn.commit()


def missing() -> list[dict]:
    return [p for p in catalog.PRESENTERS if not path_for(p["id"])]


def start(conn) -> str:
    """Start a still for every presenter that has none (and none on the way), within the cap."""
    st = state(conn)
    started, skipped = [], []
    for pr in missing():
        if st["jobs"].get(pr["id"], {}).get("status") == "running":
            continue
        inp = inputs(pr)
        est = hf.estimate(MODEL, inp)
        usd = est if est is not None else FALLBACK_USD
        if st["spent"] + usd > CAP_USD + 1e-9:
            skipped.append(pr["name"])
            continue
        rid = hf.submit(MODEL, inp)
        st["spent"] = round(st["spent"] + usd, 4)
        st["jobs"][pr["id"]] = {"rid": rid, "usd": round(usd, 4), "status": "running", "estimated": est is not None}
        started.append(pr["name"])
        _save_state(conn, st)
    _save_state(conn, st)
    msg = f"Started {len(started)} still{'s' if len(started) != 1 else ''}" + (f": {', '.join(started)}." if started else ".")
    if skipped:
        msg += f" Not started (would pass the ${CAP_USD:.2f} cap): {', '.join(skipped)}."
    return msg


def collect(conn, fetch=None) -> int:
    """Save finished stills; failed ones are uncounted. Returns how many were saved."""
    import requests
    fetch = fetch or (lambda url: requests.get(url, timeout=120).content)
    st = state(conn)
    saved = 0
    for pid, job in st["jobs"].items():
        if job.get("status") != "running":
            continue
        try:
            res = hf.status(job["rid"])
        except Exception:  # noqa: BLE001
            continue
        s = (res.get("status") or "").lower()
        if s == "completed":
            outs = [o for o in hf.outputs(res) if o["kind"] == "image"]
            if not outs:
                job["status"] = "failed"
                continue
            from PIL import Image
            img = Image.open(io.BytesIO(fetch(outs[0]["url"]))).convert("RGB")
            img.thumbnail((600, 800))
            os.makedirs(DATA_DIR, exist_ok=True)
            img.save(os.path.join(DATA_DIR, f"{pid}.jpg"), "JPEG", quality=86)
            job["status"] = "done"
            saved += 1
        elif s in hf.FINAL:
            job["status"] = "failed"
            st["spent"] = round(max(0.0, st["spent"] - job.get("usd", 0)), 4)
    _save_state(conn, st)
    return saved

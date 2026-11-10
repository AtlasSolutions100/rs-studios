"""One free fix per delivered asset (quality wave 2). Switch: FREE_FIX, default off. No price changes.

A member who got a picture that isn't right can ask for it to be made again, once, free, with a short note on
what to fix ("the cap should be black", "less clutter behind it"). Images only for now.

What a fix is: a new job for ONE picture with the same settings as the original (product photos, scene, words,
shape, preset), 0 credits, the note added to the prompt as a correction (never as words on the image), tied to
the original through jobs.parent_job_id and an asset_fixes row. It appears in the same batch, labelled "fix".

Limits:
  * one fix per delivered picture (asset_fixes.output_id); a fix is not itself fixable; free previews and
    failed or refunded ads have nothing to fix;
  * a cap per workspace per calendar month (UTC): workspaces.free_fix_cap when an admin set one for that
    workspace, else FREE_FIX_MONTHLY_CAP (default 10);
  * a fix that fails on our side (vendor error, the automatic check) is released: it can be asked for again and
    doesn't count against the month.

What a fix costs us (the member pays nothing): one image generation, catalog.TOOLS["ad_image"]["usd_each"] =
$0.1071, plus one automatic check (about $0.004) when IMAGE_QA is on, and at worst one silent retry on top
($0.111). The original picture sold for 22 credits ($0.22 at face value), so a fix takes about half of what that
picture brought in and the retry case nearly all of it. The monthly cap bounds it: 10 fixes cost at most
about $1.11 a workspace a month ($2.22 if every one also needed its retry). cost() returns these numbers.
"""
import json
import os

import catalog
import compliance
import db

DEFAULT_MONTHLY_CAP = 10
FAIL_TEXT = "We couldn't get the fix right. It wasn't counted."     # a fix that failed the automatic check
NOTE_MAX = 300


class FixError(ValueError):
    """Why a fix can't start; the message is for the member."""


def on() -> bool:
    return os.environ.get("FREE_FIX", "0").strip().lower() in ("1", "on", "true", "yes")


def default_cap() -> int:
    try:
        return max(0, int(os.environ.get("FREE_FIX_MONTHLY_CAP", str(DEFAULT_MONTHLY_CAP))))
    except ValueError:
        return DEFAULT_MONTHLY_CAP


def cap(ws) -> int:
    """This workspace's free fixes a month: its own setting when it has one, else the default."""
    own = ws["free_fix_cap"] if ws is not None and "free_fix_cap" in ws.keys() else None
    return max(0, int(own)) if own is not None else default_cap()


def cost() -> dict:
    """What one fix costs us, and the most a workspace can cost in a month (docs, the admin page)."""
    import image_qa
    image = catalog.TOOLS["ad_image"]["usd_each"]
    check = image_qa.cost()["check_usd"] if image_qa.on() else 0.0
    return {"fix_usd": round(image + check, 4), "worst_usd": round(2 * (image + check), 4),
            "monthly_cap": default_cap(), "monthly_usd": round(default_cap() * (image + check), 2),
            "monthly_worst_usd": round(default_cap() * 2 * (image + check), 2)}


def _month_start() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-01")


def used_this_month(conn, ws_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM asset_fixes WHERE workspace_id=? AND status='used' AND created_at >= ?",
                        (ws_id, _month_start())).fetchone()[0]


def _params(job) -> dict:
    try:
        return json.loads(job["params_json"] or "{}")
    except (ValueError, TypeError):
        return {}


def fixable(job) -> bool:
    """Is this job the kind a free fix exists for at all (before looking at what's been used)?"""
    if job is None or job["tool"] != "ad_image" or job["status"] != "done":
        return False
    p = _params(job)
    return not (p.get("preview") or p.get("free_fix") or p.get("service")) and int(job["credits"] or 0) > 0


def status(conn, ws, job, output_id=None) -> dict:
    """{"ok", "why", "left", "outputs": [ids that still have their fix]} for one job."""
    out = {"ok": False, "why": "", "left": 0, "outputs": []}
    if not on():
        out["why"] = "off"
        return out
    if not fixable(job):
        out["why"] = "This ad doesn't have a free fix."
        return out
    left = cap(ws) - used_this_month(conn, ws["id"])
    out["left"] = max(0, left)
    used = {r["output_id"] for r in conn.execute("SELECT output_id FROM asset_fixes WHERE job_id=? AND status='used'",
                                                 (job["id"],))}
    outs = [r["id"] for r in conn.execute("SELECT id FROM outputs WHERE job_id=? AND kind='image' ORDER BY id",
                                          (job["id"],))]
    out["outputs"] = [o for o in outs if o not in used]
    if output_id is not None and output_id not in outs:
        out["why"] = "That picture isn't part of this ad."
    elif output_id is not None and output_id in used:
        out["why"] = "This picture's free fix has been used."
    elif not out["outputs"]:
        out["why"] = "This ad's free fix has been used."
    elif left <= 0:
        out["why"] = f"This month's {cap(ws)} free fixes are used. They come back on the 1st."
    else:
        out["ok"] = True
    return out


def offer(conn, ws, job) -> dict | None:
    """For the ad's card: {"output", "left"} when a fix can be asked for, else None."""
    if not on() or not fixable(job):
        return None
    st = status(conn, ws, job)
    return {"output": st["outputs"][0], "left": st["left"], "cap": cap(ws)} if st["ok"] else None


def clean_note(note: str) -> str:
    note = " ".join(str(note or "").split())[:NOTE_MAX]
    if len(note) < 4:
        raise FixError("Say in a few words what to fix.")
    chk = compliance.check(note)
    if not chk["ok"]:
        raise FixError("That note breaks the rules: " + " ".join(chk["blocks"]))
    return note


def start(conn, ws, user_id: int, job, output_id: int, note: str) -> int:
    """Queue the free fix of one picture; returns the new job's id. Raises FixError. No credits move."""
    st = status(conn, ws, job, output_id)
    if not st["ok"]:
        raise FixError(st["why"] if st["why"] != "off" else "Free fixes aren't switched on.")
    if ws["billing_hold"] if "billing_hold" in ws.keys() else 0:
        raise FixError("This workspace is paused: a payment was reversed.")
    note = clean_note(note)
    p = _params(job)
    for k in ("qa_attempt", "qa_want", "line"):
        p.pop(k, None)
    p.update(batch=1, free_fix=True, fix_of=job["id"], fix_output=output_id, fix_note=note,
             label=((p.get("label") or "Image ad").split(" · fix")[0] + " · fix"))
    usd = catalog.cost_usd("ad_image", batch=1)
    t = db.now()
    # the allowance is taken first: a unique index on (output_id) for used fixes means two clicks can't both get it
    import sqlite3
    try:
        fid = conn.execute("INSERT INTO asset_fixes (workspace_id, job_id, output_id, user_id, note, cost_usd, status, "
                           "created_at) VALUES (?,?,?,?,?,?,'used',?)",
                           (job["workspace_id"], job["id"], output_id, user_id, note, round(usd, 4), t)).lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        raise FixError("This picture's free fix has been used.") from None
    jid = conn.execute(
        "INSERT INTO jobs (workspace_id, user_id, tool, template_id, brief, params_json, status, credits, cost_usd, "
        "created_at, updated_at, priority, brand_id, product_id, batch_id, parent_job_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job["workspace_id"], user_id, "ad_image", job["template_id"], job["brief"], json.dumps(p), "pending", 0,
         round(usd, 4), t, t, job["priority"] or 0, job["brand_id"], job["product_id"], job["batch_id"], job["id"])).lastrowid
    conn.execute("UPDATE asset_fixes SET fix_job_id=? WHERE id=?", (jid, fid))
    conn.commit()
    return jid


def release(conn, fix_job_id: int) -> bool:
    """The fix job failed: its allowance comes back (called by the worker; the caller commits)."""
    cur = conn.execute("UPDATE asset_fixes SET status='released' WHERE fix_job_id=? AND status='used'", (fix_job_id,))
    return cur.rowcount > 0


def stats(conn, days: int = 30) -> dict:
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) usd, COUNT(DISTINCT workspace_id) ws FROM asset_fixes "
                     "WHERE status='used' AND created_at >= ?", (since,)).fetchone()
    return {"fixes": r["n"], "usd": round(r["usd"], 2), "workspaces": r["ws"]}

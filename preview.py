"""The free talking-video preview (owner decision D2).

A free account gets ONE 8-second talking video at 480p, watermarked, instead of
video credits: the first-run "wow". It costs us the vendor's 480p rate
($0.2056/s, about $1.64) and the member nothing. Limits: one per workspace and
one per email address (normalised: case, +tags and Gmail dots don't make a new
address), plus the signup IP throttles and a daily cap across all accounts
(RS_FREE_PREVIEWS_PER_DAY). A preview that fails on our side can be tried again
(up to MAX_TRIES). RS_FREE_PREVIEW=0 switches it off.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

import catalog
import plans

SECONDS = 8
RESOLUTION = "480p"
USD_PER_SECOND = float(os.environ.get("RS_PREVIEW_USD_PER_SECOND", "0.2056"))
MAX_TRIES = 3
DEFAULT_LINE = "Meet {product}. Let me show you what it is and why it's worth a look."
PRESENTERS = ("maya", "jordan", "priya", "marcus")


def enabled() -> bool:
    return os.environ.get("RS_FREE_PREVIEW", "1") != "0" and catalog.capabilities()["talking"]


def daily_cap() -> int:
    try:
        return int(os.environ.get("RS_FREE_PREVIEWS_PER_DAY", "150"))
    except ValueError:
        return 150


def cost_usd() -> float:
    return round(USD_PER_SECOND * SECONDS, 4)


def norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def ip_hash(ip: str) -> str:
    return hashlib.sha256(("cfy-preview:" + (ip or "")).encode()).hexdigest()[:24]


def status(conn, ws, email: str) -> dict:
    """{"ok": bool, "why": str}: can this workspace (and this login's email) make its free preview now?"""
    if not enabled():
        return {"ok": False, "why": "off"}
    if plans.plan_code(ws) != "free":
        return {"ok": False, "why": "paid"}
    rows = conn.execute("SELECT * FROM free_previews WHERE workspace_id=? OR email_norm=?",
                        (ws["id"], norm_email(email))).fetchall()
    for r in rows:
        if r["job_id"] or r["tries"] >= MAX_TRIES:
            return {"ok": False, "why": "used"}
    since = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    made = conn.execute("SELECT COUNT(*) FROM free_previews WHERE created_at>=? AND job_id IS NOT NULL",
                        (since,)).fetchone()[0]
    if made >= daily_cap():
        return {"ok": False, "why": "busy"}
    return {"ok": True, "why": ""}


def claim(conn, ws_id: int, email: str, ip: str, job_id: int, now: str) -> bool:
    """Record the preview against the workspace and the email (no commit)."""
    em = norm_email(email)
    row = conn.execute("SELECT * FROM free_previews WHERE workspace_id=? OR email_norm=?", (ws_id, em)).fetchone()
    if row:
        if row["job_id"] or row["tries"] >= MAX_TRIES:
            return False
        conn.execute("UPDATE free_previews SET job_id=?, tries=tries+1, created_at=? WHERE id=?", (job_id, now, row["id"]))
        return True
    conn.execute("INSERT INTO free_previews (workspace_id, email_norm, ip_hash, job_id, tries, created_at) "
                 "VALUES (?,?,?,?,1,?)", (ws_id, em, ip_hash(ip), job_id, now))
    return True


def release(conn, job_id: int):
    """The preview failed on our side: let the member try again (counted in tries)."""
    conn.execute("UPDATE free_previews SET job_id=NULL WHERE job_id=?", (job_id,))


def line_for(product: str) -> str:
    return DEFAULT_LINE.replace("{product}", product or "this")

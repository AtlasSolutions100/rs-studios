"""Storage quotas, request body limits and the disk guard (roadmap M1.4; SEC-1, SEC-2).

* Each workspace may keep up to its plan's quota on our disk: Free 200 MB,
  Starter 2 GB, Growth 10 GB, Scale 50 GB. Counted: its product photos
  (uploads/<ws>), Creator Pool files (uploads/creator_pools/<ws>) and finished
  ads (media/<ws>/<job>), not counting ads the member deleted. Checked before an
  upload is saved (against the request's size) and again with the real size;
  a finished ad over quota keeps only the vendor's link (it isn't lost).
  RS_STORAGE_QUOTAS=0 switches quotas off.
* Body limits per route (checked from Content-Length before anything reads the
  body, and enforced on the stream for chunked bodies): upload routes 155 MB,
  the contacts CSV 5 MB, webhooks 256 KB, everything else 1 MB.
* When free disk where the data lives is under RS_DISK_MIN_GB (5 GB), new files
  are refused with 507 and the operator is alerted (alerts.py).
* purge() (once a day, from the worker): leftover temp files of conversions and
  watermarking, and platform exports older than RS_EXPORT_KEEP_DAYS (7; they are
  made again on the next download). Deleting the files of ads members deleted
  over 30 days ago is behind RS_STORAGE_PURGE_DELETED=1 (off by default).
"""
import logging
import os
import re
import time

import db

log = logging.getLogger("campaignify")

MB = 1024 * 1024
QUOTA_MB = {"free": 200, "starter": 2 * 1024, "growth": 10 * 1024, "scale": 50 * 1024}

UPLOAD_MB = 155
BODY_LIMITS = {   # endpoint -> bytes; anything not listed gets DEFAULT_BODY
    "start": UPLOAD_MB * MB, "upload_photos": UPLOAD_MB * MB, "admin_refs": UPLOAD_MB * MB,
    "products.new": UPLOAD_MB * MB, "products.edit": UPLOAD_MB * MB, "cpools.index": UPLOAD_MB * MB,
    "messaging.import_contacts": 5 * MB,
    "stripe_webhook": 256 * 1024, "messaging.resend_webhook": 256 * 1024, "sms.inbound": 256 * 1024,
    "sms.status": 256 * 1024, "cpools.partner_event": 256 * 1024, "cpools.partner_asset_url": 256 * 1024,
}
DEFAULT_BODY = 1 * MB
PURGE_META = "storage_purge_at"


class StorageFull(Exception):
    """Refused for space. `status` is 507 for both: the workspace's quota or the server's disk."""
    status = 507


class QuotaFull(StorageFull):
    pass


class DiskFull(StorageFull):
    pass


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def quotas_on() -> bool:
    return os.environ.get("RS_STORAGE_QUOTAS", "1") != "0"


def uploads_dir() -> str:
    return os.path.join(db.DATA, "uploads")


def media_dir() -> str:
    return os.path.join(db.DATA, "media")


def quota_bytes(ws) -> int:
    import plans
    return QUOTA_MB.get(plans.plan_code(ws), QUOTA_MB["free"]) * MB


def _size(path: str) -> int:
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        total += _size(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def used_bytes(ws_id: int, conn=None) -> int:
    """What a workspace keeps on our disk now (deleted ads don't count)."""
    ws_id = int(ws_id)
    total = _size(os.path.join(uploads_dir(), str(ws_id))) + _size(os.path.join(uploads_dir(), "creator_pools", str(ws_id)))
    own = conn is None
    conn = conn or db.connect()
    try:
        deleted = {str(r[0]) for r in conn.execute("SELECT id FROM jobs WHERE workspace_id=? AND deleted_at IS NOT NULL",
                                                   (ws_id,))}
    finally:
        if own:
            conn.close()
    folder = os.path.join(media_dir(), str(ws_id))
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name in deleted:
                    continue
                try:
                    total += _size(e.path) if e.is_dir(follow_symlinks=False) else e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def fmt_size(n: int) -> str:
    if n >= 1024 * MB:
        v = n / (1024 * MB)
        return f"{v:.1f} GB".replace(".0 GB", " GB")
    if n < MB:
        return f"{max(0, round(n / 1024))} KB"
    return f"{round(n / MB)} MB"


def quota_text(ws, used: int | None = None) -> str:
    import plans
    used = used_bytes(ws["id"]) if used is None else used
    name = plans.PLANS.get(plans.plan_code(ws), {}).get("name", "Free")
    return f"{fmt_size(used)} of {fmt_size(quota_bytes(ws))} used on the {name} plan"


# ---------------------------------------------------------------- the disk guard

def disk_free_bytes() -> int:
    import shutil
    try:
        return shutil.disk_usage(db.DATA).free
    except OSError:
        return -1


DISK_MESSAGE = ("Our server is out of space for new files right now, so this couldn't be saved. Nothing was "
                "charged. Try again in a little while; we've been told.")


def disk_guard():
    """Raise DiskFull (and alert the operator) when free disk is under RS_DISK_MIN_GB."""
    free = disk_free_bytes()
    if 0 <= free < _num("RS_DISK_MIN_GB", 5) * 1024 * MB:
        try:
            import alerts
            alerts.fire("disk_full", f"Disk full: new files refused ({free / (1024 * MB):.1f} GB free)",
                        f"Uploads and new files are refused below {_num('RS_DISK_MIN_GB', 5):g} GB free. "
                        "Free space on the server (shared with Crowdcut).")
        except Exception:  # noqa: BLE001
            log.exception("couldn't raise the disk alert")
        raise DiskFull(DISK_MESSAGE)


def quota_message(ws, used: int) -> str:
    return (f"Your storage is full: {quota_text(ws, used)}. Delete photos or ads you don't need, "
            "or move to a bigger plan on Billing, then try again.")


def guard(ws, incoming: int = 0, used: int | None = None):
    """Before writing `incoming` bytes for a workspace (None: no quota, e.g. admin files)."""
    disk_guard()
    if ws is None or not quotas_on():
        return
    used = used_bytes(ws["id"]) if used is None else used
    if used + max(0, int(incoming or 0)) > quota_bytes(ws):
        raise QuotaFull(quota_message(ws, used))


def check_upload(ws):
    """Once per request, before the first file of an upload is saved: the whole
    body's size against what's left (a 20-photo upload is refused before any is kept)."""
    from flask import g, has_request_context, request
    if not has_request_context():
        guard(ws, 0)
        return
    if g.get("_storage_checked"):
        disk_guard()
        return
    guard(ws, request.content_length or 0)
    g._storage_checked = True


def check_saved(ws, path: str):
    """After a file is on disk: if it took the workspace over quota, remove it and refuse."""
    if ws is None or not quotas_on():
        return
    used = used_bytes(ws["id"])
    if used > quota_bytes(ws):
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            size = 0
        raise QuotaFull(quota_message(ws, used - size))


# ---------------------------------------------------------------- body limits

def body_limit(endpoint) -> int:
    return BODY_LIMITS.get(endpoint or "", DEFAULT_BODY)


def install(app):
    """Body limits per route (before anything parses the body) and the 413/507 pages."""
    from flask import jsonify, render_template, request

    @app.before_request
    def _body_limit():
        limit = body_limit(request.endpoint)
        request.max_content_length = limit      # enforced on the stream too (chunked bodies)
        n = request.content_length
        if n is not None and n > limit:
            from werkzeug.exceptions import RequestEntityTooLarge
            raise RequestEntityTooLarge()

    def _wants_json():
        p = request.path
        return p.startswith(("/webhooks/", "/api/", "/mcp", "/e")) or request.accept_mimetypes.best == "application/json" \
            or request.headers.get("X-Requested-With") == "fetch"

    @app.errorhandler(413)
    def _too_large(e):
        limit = body_limit(request.endpoint)
        msg = (f"That's too big to send in one go (the limit here is {fmt_size(limit)}). "
               + ("Upload fewer or smaller files at a time." if limit >= UPLOAD_MB * MB else ""))
        if _wants_json():
            return jsonify({"error": "too_large", "message": msg.strip()}), 413
        return render_template("error.html", code=413, message=msg.strip()), 413

    @app.errorhandler(StorageFull)
    def _full(e):
        if _wants_json():
            return jsonify({"error": "storage_full", "message": str(e)}), 507
        return render_template("error.html", code=507, message=str(e)), 507


# ---------------------------------------------------------------- the daily purge

TEMP_RX = re.compile(r"\.(part\.mp4|wm\.mp4)$")


def _export_rx():
    import adhub
    pairs = sorted({f"{re.escape(k)}-{re.escape(e[0])}" for k, p in adhub.PLATFORMS.items() for e in p.get("exports", [])})
    if not pairs:
        return None
    return re.compile(r"\.(?:" + "|".join(pairs) + r")\.(?:png|jpe?g|webp|mp4)$")


def purge(now: float | None = None) -> dict:
    """Remove temp leftovers (over 1 hour old) and expired exports; with
    RS_STORAGE_PURGE_DELETED=1 also the files of ads deleted over 30 days ago."""
    now = now or time.time()
    keep_s = _num("RS_EXPORT_KEEP_DAYS", 7) * 86400
    out = {"temp": 0, "exports": 0, "deleted_jobs": 0, "bytes": 0}
    rx = _export_rx()
    for root, _dirs, files in os.walk(media_dir()):
        for name in files:
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            kind = None
            if TEMP_RX.search(name) and now - st.st_mtime > 3600:
                kind = "temp"
            elif rx and rx.search(name) and now - max(st.st_mtime, st.st_atime) > keep_s:
                kind = "exports"
            if kind:
                try:
                    os.remove(p)
                    out[kind] += 1
                    out["bytes"] += st.st_size
                except OSError:
                    pass
    if os.environ.get("RS_STORAGE_PURGE_DELETED", "0") == "1":
        import shutil
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_num("RS_PURGE_DELETED_DAYS", 30))).replace(
            microsecond=0).isoformat()
        with db.connect() as conn:
            for j in conn.execute("SELECT id, workspace_id FROM jobs WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                                  (cutoff,)).fetchall():
                folder = os.path.join(media_dir(), str(j["workspace_id"]), str(j["id"]))
                if os.path.isdir(folder):
                    size = _size(folder)
                    shutil.rmtree(folder, ignore_errors=True)
                    out["deleted_jobs"] += 1
                    out["bytes"] += size
                conn.execute("UPDATE outputs SET path=NULL WHERE job_id=? AND path IS NOT NULL", (j["id"],))
            conn.commit()
    if any(out[k] for k in ("temp", "exports", "deleted_jobs")):
        log.info("storage purge: %s", out)
    return out


def maybe_purge():
    """From the worker loop: purge once a day, around 09:00 UTC (night in the US); never raises."""
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (PURGE_META,)).fetchone()
        last = float(row["value"]) if row and row["value"] else 0.0
        t = time.time()
        hour = time.gmtime(t).tm_hour
        if not ((t - last > 20 * 3600 and hour == 9) or t - last > 48 * 3600):
            return
        with db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (PURGE_META, str(t)))
            conn.commit()
        purge(t)
    except Exception:  # noqa: BLE001
        log.exception("storage purge failed")

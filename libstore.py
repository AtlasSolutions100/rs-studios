"""Gallery media that lives outside the deploy bundle (Nov 12 2026).

Why. deploy.yml sends the whole repo to the deploy hook as one JSON body, and the hook takes at most 64 MiB
(tests/test_bundle_size.py). Pictures and clips under static/ are nearly all of that, so the gallery could not grow:
release m35 added 36 MB of examples and main could not be deployed. A clip listed here is not in the bundle. It sits on
the server's data volume, and the server fetches it once, by itself.

How. An entry of static/showcase/manifest.json with a "store" block is a store item:

    {"file": "ex-trainer-presenter.mp4", "poster": "ex-trainer-presenter.webp", ...,
     "store": {"sha256": "<64 hex>", "bytes": 373110}}

and the manifest's "_store" says where store files come from: {"commit": "<40 hex>", "dir": "assets_staging/..."}.
The file is read from this repository's own public tree at exactly that commit:

    https://raw.githubusercontent.com/AtlasSolutions100/rs-studios/<commit>/<dir>/<file>

and kept only when its size and SHA-256 are the ones the manifest names. So nothing reaches the server that was not
reviewed in a pull request: the commit and every hash are in the repo, the origin is fixed in this file, and a file
that differs by one byte is refused. The poster stays in static/ (it is small, and the tile needs it at once).

    wanted()       the store items the manifest lists: [{"name", "sha256", "bytes", "url"}]
    have(name)     the path of a store file that is on the volume (right size), else None
    url(name)      its public address, /lib/<name>?v=<first 12 of the hash>
    missing()      the wanted files that are not there yet
    sync()         fetch what is missing, check it, keep it; {"fetched", "kept", "failed"}. Safe to call again.
    version()      changes whenever the store's files do (public_site.showcase reads the manifest again then)

When. The worker's mail loop calls tick() every minute: it does nothing while nothing is missing, tries at once after a
deploy, and after a failed try waits RETRY_S before the next. A store item whose file is not there yet is simply left
out of the gallery (the rule the manifest always had for a missing file), so a slow or failed fetch never breaks a
page. A site admin sees the count on /admin/library and can press "Fetch them now".

GET /lib/<name> serves a store file, and only a name the manifest lists. The address carries the hash, so it is
cached for a year.

The commit must stay reachable in the repository: keep the assets branch it is on. LIB_STORE=0 switches the store off
(its items then never show).
"""
import hashlib
import json
import logging
import os
import re
import threading
import time

from flask import Blueprint, abort, send_file

import db

log = logging.getLogger("campaignify.libstore")

bp = Blueprint("libstore", __name__)

REPO_RAW = "https://raw.githubusercontent.com/AtlasSolutions100/rs-studios"
MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "showcase", "manifest.json")
EXT = (".mp4", ".webm", ".webp", ".jpg", ".jpeg", ".png", ".mp3")
MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".mp3": "audio/mpeg"}
MAX_FILE = 8 * 1024 * 1024          # one store file
MAX_FILES = 400
RETRY_S = 600                       # after a try that left something missing
PER_TICK = 40                       # files fetched in one pass
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_HEX40, _HEX64 = re.compile(r"^[0-9a-f]{40}$"), re.compile(r"^[0-9a-f]{64}$")
_DIR = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_lock = threading.Lock()
_state = {"key": None, "wanted": [], "next_try": 0.0}


def enabled() -> bool:
    return os.environ.get("LIB_STORE", "1") != "0"


def folder() -> str:
    return os.path.join(db.DATA, "libstore")


def wanted() -> list[dict]:
    """The store items the manifest lists, read again when the manifest changes. An entry that is not exactly
    right (a name with a path in it, a hash that is not a hash, a size over the limit) is left out and logged."""
    try:
        key = (MANIFEST, os.path.getmtime(MANIFEST))
    except OSError:
        return []
    if _state["key"] == key:
        return list(_state["wanted"])
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    src = raw.get("_store") if isinstance(raw, dict) else None
    commit = str((src or {}).get("commit") or "")
    sub = str((src or {}).get("dir") or "").strip("/")
    out, seen = [], set()
    if isinstance(src, dict) and _HEX40.match(commit) and _DIR.match(sub) and ".." not in sub.split("/"):
        for it in raw.get("items") or []:
            st = it.get("store") if isinstance(it, dict) else None
            if not isinstance(st, dict):
                continue
            name, sha, size = str(it.get("file") or ""), str(st.get("sha256") or ""), st.get("bytes")
            ok = (_NAME.match(name) and name.lower().endswith(EXT) and _HEX64.match(sha) and isinstance(size, int)
                  and not isinstance(size, bool) and 0 < size <= MAX_FILE and name not in seen)
            if not ok or len(out) >= MAX_FILES:
                log.warning("store entry left out (name, hash or size not right): %s", name[:80])
                continue
            seen.add(name)
            out.append({"name": name, "sha256": sha, "bytes": size, "url": f"{REPO_RAW}/{commit}/{sub}/{name}"})
    elif any(isinstance(it, dict) and it.get("store") for it in (raw.get("items") or []) if isinstance(raw, dict)):
        log.warning("the manifest has store items but no usable _store (commit and dir)")
    _state.update(key=key, wanted=out, next_try=0.0)
    return list(out)


def _entry(name: str):
    return next((w for w in wanted() if w["name"] == name), None)


def have(name: str):
    """The path of a store file that is on the volume, with the size the manifest names; else None."""
    w = _entry(str(name or "")) if enabled() else None
    if w is None:
        return None
    path = os.path.join(folder(), w["name"])
    try:
        return path if os.path.getsize(path) == w["bytes"] else None
    except OSError:
        return None


def url(name: str) -> str:
    w = _entry(str(name or ""))
    return f"/lib/{w['name']}?v={w['sha256'][:12]}" if w else ""


def missing() -> list[dict]:
    return [w for w in wanted() if have(w["name"]) is None] if enabled() else []


def version() -> tuple:
    """Changes when a store file arrives or goes: what is there, by name and size."""
    return tuple((w["name"], have(w["name"]) is not None) for w in wanted())


def _fetch(url_: str, max_bytes: int) -> bytes:
    import fetcher
    return fetcher.fetch_bytes(url_, max_bytes=max_bytes, accept="*/*")["data"]


def sync(fetch=None, limit: int = PER_TICK) -> dict:
    """Fetch the wanted files that are missing, check each against the manifest and keep the ones that match.
    One at a time under a lock (a second caller returns at once). Never raises."""
    out = {"fetched": 0, "kept": 0, "failed": []}
    if not enabled():
        return out
    if not _lock.acquire(blocking=False):
        return out
    try:
        todo = missing()
        out["kept"] = len(wanted()) - len(todo)
        if not todo:
            return out
        os.makedirs(folder(), exist_ok=True)
        get = fetch or _fetch
        for w in todo[:max(0, limit)]:
            try:
                data = get(w["url"], w["bytes"])
                if len(data) != w["bytes"] or hashlib.sha256(data).hexdigest() != w["sha256"]:
                    raise ValueError("not the file the manifest names (size or hash)")
                tmp = os.path.join(folder(), f".{w['name']}.{os.getpid()}.part")
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, os.path.join(folder(), w["name"]))
                out["fetched"] += 1
            except Exception as e:  # noqa: BLE001  one bad file never stops the others, or the caller
                out["failed"].append(w["name"])
                log.warning("store file %s not fetched: %s", w["name"], str(e)[:160])
        if out["fetched"] or out["failed"]:
            log.info("library store: %s fetched, %s already there, %s failed", out["fetched"], out["kept"],
                     len(out["failed"]))
        return out
    finally:
        _lock.release()


def tick(now=None) -> dict | None:
    """The background pass: nothing while nothing is missing; else one sync, then RETRY_S before the next try."""
    now = time.time() if now is None else now
    if not enabled() or not missing() or now < _state["next_try"]:
        return None
    out = sync()
    _state["next_try"] = now + RETRY_S if missing() else 0.0
    return out


def status() -> dict:
    """For the admin page: how many store files the manifest lists and how many are on the volume."""
    w = wanted()
    gone = missing()
    return {"on": enabled(), "wanted": len(w), "there": len(w) - len(gone), "missing": [m["name"] for m in gone][:12]}


@bp.route("/lib/<name>")
def file(name):
    path = have(name)
    if path is None:
        abort(404)
    ext = os.path.splitext(name)[1].lower()
    return send_file(path, mimetype=MIME.get(ext), conditional=True, max_age=365 * 86400)

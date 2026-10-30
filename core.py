"""Helpers every page module shares: access guards, the active brand, and
charging credits for AI calls. Blueprints import from here (never from app.py)."""
import functools
import json
import mimetypes
import os
import uuid

from flask import abort, flash, g, redirect, render_template, request, url_for

import db
import plans
import products


UPLOADS = os.path.join(db.DATA, "uploads")
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
MAX_PHOTO_MB = 25
MAX_VIDEO_MB = 150


MAX_PHOTO_PIXELS = 50_000_000      # ~8K x 6K; anything bigger is a decompression bomb, not a product photo
_PIL_TYPES = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


def _check_photo(path: str):
    """(content type, extension) of a real JPEG, PNG or WebP, read from the
    file itself (the browser's label can say anything). Raises ValueError."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            w, h = im.size
            if fmt not in _PIL_TYPES:
                raise ValueError("Use a JPEG, PNG or WebP photo.")
            if w * h > MAX_PHOTO_PIXELS:
                raise ValueError("That photo is too large in pixels. Use one under 50 megapixels.")
            im.load()      # a truncated or corrupt file fails here, not at the generation API
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — Pillow raises many kinds for broken files
        raise ValueError("That file isn't a photo we can read. Use a JPEG, PNG or WebP.") from None
    return _PIL_TYPES[fmt]


def save_upload(fs, folder, allowed, max_mb):
    ctype = fs.mimetype or mimetypes.guess_type(fs.filename or "")[0] or ""
    name = (fs.filename or "").lower()
    if ctype not in allowed:
        if ctype in ("image/heic", "image/heif") or name.endswith((".heic", ".heif")):
            raise ValueError("iPhone HEIC photos aren't supported yet. Export it as a JPEG (on iPhone: Settings > "
                             "Camera > Formats > Most Compatible, or share it as JPEG) and upload that.")
        raise ValueError("That file type isn't supported.")
    os.makedirs(folder, exist_ok=True)
    ext = mimetypes.guess_extension(ctype) or os.path.splitext(fs.filename or "")[1] or ""
    path = os.path.join(folder, f"{uuid.uuid4().hex}{ext}")
    fs.save(path)
    if os.path.getsize(path) > max_mb * 1024 * 1024:
        os.remove(path)
        raise ValueError(f"Files up to {max_mb} MB.")
    if ctype.startswith("image/"):
        try:
            real, real_ext = _check_photo(path)
        except ValueError:
            os.remove(path)
            raise
        if real != ctype:       # a PNG labelled JPEG still works; store and serve it as what it is
            fixed = os.path.splitext(path)[0] + real_ext
            os.replace(path, fixed)
            path, ctype = fixed, real
    return path, ctype



def login_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrap


def admin_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not g.user["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrap


def feature_required(feature):
    """Logged in and on a plan that includes `feature`; otherwise an upgrade page."""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if not plans.has(g.ws, feature):
                return render_template("upgrade.html", feature=plans.FEATURES[feature],
                                       need=plans.required(feature)), 402
            return fn(*a, **kw)
        return wrap
    return deco


def is_owner() -> bool:
    return bool(g.user) and (g.user["role"] == "owner" or bool(g.user["is_admin"]))


def owner_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not is_owner():
            abort(403, "Only the workspace owner can do that.")
        return fn(*a, **kw)
    return wrap


def brand(product_id=None):
    """The brief every flow starts from: the active brand and product, or the
    given product of this workspace (a flow's product picker)."""
    if product_id and str(product_id) != str(g.ws["product_id"] or ""):
        with db.connect() as conn:
            return products.brief_for(conn, g.ws, product_id)
    return products.as_brief(g.get("brand_row"), g.get("product_row"), g.ws)


def picked_product():
    """The product id a form or query string picked, if it is this workspace's."""
    raw = request.values.get("product") or request.values.get("product_id")
    if not raw or not str(raw).isdigit():
        return None
    with db.connect() as conn:
        p = products.get_product(conn, g.ws["id"], int(raw))
        if p is None or p["brand_id"] not in products.unlocked_brand_ids(conn, g.ws):
            return None
    return p["id"]


def charge_ai(conn, credits: int, reason: str) -> int:
    """Take what an AI call actually cost (at most what's left). Returns credits taken."""
    if credits <= 0:
        return 0
    have = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone())
    take = min(credits, have)
    if take > 0:
        db.adjust_credits(conn, g.ws["id"], -take, reason)
    return take


def can_afford(credits: int) -> bool:
    return db.balance(g.ws) >= credits


def not_enough(credits: int):
    flash(f"That needs about {credits:,} credits and you have {db.balance(g.ws):,}. Top up on the Billing page.")


def verify_block(conn):
    """The friendly message when this member must confirm their email before
    spending free credits (verify.py); None when they may go ahead."""
    import verify
    if g.get("user") is None or g.get("ws") is None:
        return None
    return verify.message(g.user["email"]) if verify.blocked(conn, g.ws, g.user) else None


def queue_job(conn, tool, tpl_id, brief, params, usd, credits, quiet=False, product_id=None, batch_id=None):
    """Insert a pending job and take its credits in one transaction. None (and a
    message) when the workspace can't afford it, or when a free account hasn't
    confirmed its email yet (then g.verify_blocked holds the message and nothing
    uncommitted on `conn` is kept). The job is tagged with its product (default:
    the active one) and that product's brand."""
    msg = verify_block(conn)
    if msg:
        conn.rollback()
        g.verify_blocked = msg
        if not quiet:
            flash(msg, "verify")
        return None
    now = db.now()
    pid = product_id or g.ws["product_id"]
    p = products.get_product(conn, g.ws["id"], pid) if pid else None
    bid = p["brand_id"] if p is not None else g.ws["brand_id"]
    cur = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, template_id, brief, params_json, "
                       "status, credits, cost_usd, created_at, updated_at, priority, brand_id, product_id, batch_id) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (g.ws["id"], g.user["id"], tool, tpl_id, brief, json.dumps(params), "pending",
                        credits, round(usd, 4), now, now, plans.priority(g.ws), bid, p["id"] if p is not None else None,
                        batch_id))
    jid = cur.lastrowid
    if not db.adjust_credits(conn, g.ws["id"], -credits, "generation", jid):
        conn.rollback()
        if not quiet:
            flash(f"That needs {credits:,} credits and you have {db.balance(g.ws):,}. Buy a credit pack or pick a plan "
                  "on the Billing page.")
        return None
    conn.commit()
    return jid

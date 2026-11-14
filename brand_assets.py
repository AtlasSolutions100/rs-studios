"""Brand assets (Koby, Nov 13 2026): the pictures a brand wants in its ads, kept in one place and picked as references.

Product photos, packaging, lifestyle shots, a logo, a store front, a person who works there: anything an ad should
show or match. They are rows of `photos` (the table every ad already reads its product photos from), with a kind
and a label; a product photo is also tied to its product, so it is that product's photo everywhere. In Studio's
composer, "References" puts any of them into the ad: create_routes takes the ids (photos the workspace owns), the
worker sends them to the image model with the product photo, and the prompt names what each one is.

    GET  /app/brand/assets            (the Brand page's Assets section reads assets_for)
    POST /app/brand/assets            upload one or more pictures with a kind and a label
    POST /app/brand/assets/<id>/del   remove one (a product's last photo can't go: its ads start from it)
"""
import os

from flask import Blueprint, flash, g, redirect, request, url_for

import db
import products
from core import UPLOADS, login_required, save_upload

bp = Blueprint("brand_assets", __name__)

KINDS = [("product", "Product photo"), ("packaging", "Packaging"), ("lifestyle", "Lifestyle"), ("logo", "Logo"),
         ("place", "Place or store"), ("person", "Person"), ("other", "Other")]
KIND_NAME = dict(KINDS)
TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_MB = 15
MAX_FILES = 12
MAX_REFS = 4          # references picked into one ad, beside the product photo


def assets_for(conn, ws_id: int, brand_id) -> list[dict]:
    """The brand's pictures, newest first: its own assets, then its products' photos."""
    if not brand_id:
        return []
    own = conn.execute("SELECT * FROM photos WHERE workspace_id=? AND brand_id=? AND product_id IS NULL ORDER BY id DESC",
                       (ws_id, brand_id)).fetchall()
    prod = conn.execute("SELECT ph.*, p.name AS product_name FROM photos ph JOIN products p ON p.id = ph.product_id "
                        "WHERE ph.workspace_id=? AND p.brand_id=? ORDER BY ph.id DESC", (ws_id, brand_id)).fetchall()
    out = []
    for r in list(own) + list(prod):
        kind = (r["kind"] if "kind" in r.keys() else None) or ("product" if r["product_id"] else "other")
        label = (r["label"] if "label" in r.keys() else None) or (
            r["product_name"] if "product_name" in r.keys() and r["product_name"] else KIND_NAME.get(kind, "Picture"))
        out.append({"id": r["id"], "kind": kind, "kind_name": KIND_NAME.get(kind, "Other"), "label": label,
                    "product_id": r["product_id"], "url": url_for("photo", pid=r["id"])})
    return out


def describe(rows) -> str:
    """The prompt line that says what the reference pictures after the product photo are ("" when none have a kind)."""
    named = []
    for r in rows[1:]:
        kind = r["kind"] if "kind" in r.keys() else None
        if not kind:
            continue
        label = (r["label"] if "label" in r.keys() else "") or KIND_NAME.get(kind, "")
        named.append(f"{KIND_NAME.get(kind, 'picture').lower()}" + (f" ({label})" if label and label != KIND_NAME.get(kind) else ""))
    if not named:
        return ""
    return ("The other reference images are the advertiser's own: " + "; ".join(named[:MAX_REFS]) + ". Use them "
            "exactly as they are where they fit the scene (a logo stays a logo, a place stays that place); do not "
            "invent other brands.")


def _brand():
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        bid = request.form.get("brand", type=int) or ws["brand_id"]
        b = products.get_brand(conn, g.ws["id"], bid) if bid else None
    return b


@bp.route("/app/brand/assets", methods=["POST"])
@login_required
def upload():
    back = request.form.get("back") or url_for("studio_v2.brand") + "#assets"
    if not back.startswith("/"):
        back = url_for("studio_v2.brand") + "#assets"
    b = _brand()
    if b is None:
        flash("Set up your brand first.")
        return redirect(back)
    kind = request.form.get("kind") if request.form.get("kind") in KIND_NAME else "other"
    label = " ".join((request.form.get("label") or "").split())[:80]
    files = [f for f in request.files.getlist("files") if f and f.filename][:MAX_FILES]
    if not files:
        flash("Pick one or more pictures.")
        return redirect(back)
    pid = None
    with db.connect() as conn:
        if kind == "product":
            pid = request.form.get("product", type=int)
            p = products.get_product(conn, g.ws["id"], pid) if pid else None
            if p is None or p["brand_id"] != b["id"]:
                flash("Pick which product these photos are of.")
                return redirect(back)
        saved = 0
        for f in files:
            try:
                path, ctype = save_upload(f, os.path.join(UPLOADS, str(g.ws["id"]), "assets"), TYPES, MAX_MB, ws=g.ws)
            except ValueError as e:
                flash(f"{f.filename}: {e}")
                continue
            phid = products.add_photo(conn, g.ws["id"], pid, path, f.filename, ctype)
            conn.execute("UPDATE photos SET brand_id=?, kind=?, label=? WHERE id=?", (b["id"], kind, label or None, phid))
            conn.commit()
            saved += 1
    if saved:
        flash(f"{saved} picture{'' if saved == 1 else 's'} added to {b['brand_name'] or 'your brand'}.", "ok")
    return redirect(back)


@bp.route("/app/brand/assets/<int:phid>/del", methods=["POST"])
@login_required
def remove(phid):
    back = url_for("studio_v2.brand") + "#assets"
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (phid, g.ws["id"])).fetchone()
        if r is None:
            return redirect(back)
        if r["product_id"] and conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?",
                                            (r["product_id"],)).fetchone()[0] <= 1:
            flash("That's the product's only photo, and its ads start from it. Add another one first.")
            return redirect(back)
        products.remove_photo(conn, g.ws["id"], phid)
    try:
        os.remove(r["path"])
    except OSError:
        pass
    flash("Removed.", "ok")
    return redirect(back)

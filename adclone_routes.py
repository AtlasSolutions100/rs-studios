"""Clone an ad: the pages (adclone.py holds the reading and the plan)."""
import mimetypes
import os
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_from_directory, url_for

import adclone
import db
import products
from core import login_required, reserve_ai

bp = Blueprint("clone", __name__)

URL_RE = re.compile(r"^https?://", re.I)
SOCIAL = ("tiktok.com", "instagram.com", "youtube.com", "youtu.be", "facebook.com", "fb.watch", "x.com",
          "twitter.com", "linkedin.com", "snapchat.com", "pinterest.com")


def _social(link: str) -> bool:
    host = re.sub(r"^https?://(www\.)?", "", link.lower()).split("/")[0].split(":")[0]
    return any(host == s or host.endswith("." + s) for s in SOCIAL)


def _row(conn, cid):
    row = conn.execute("SELECT * FROM ad_clones WHERE id=? AND workspace_id=?", (cid, g.ws["id"])).fetchone()
    if row is None:
        abort(404)
    return row


def _product(conn):
    pid = g.ws["product_id"]
    return products.get_product(conn, g.ws["id"], pid) if pid else None


@bp.route("/app/clone", methods=["GET", "POST"])
@login_required
def page():
    with db.connect() as conn:
        adclone.ensure_table(conn)
        conn.commit()
        p = _product(conn)
        if request.method == "POST":
            return _start(conn, p)
        rows = conn.execute("SELECT * FROM ad_clones WHERE workspace_id=? ORDER BY id DESC LIMIT 12",
                            (g.ws["id"],)).fetchall()
    return render_template("v2/clone.html", p=p, quote=adclone.quote(), recent=[adclone.view(r) for r in rows],
                           max_mb=adclone.MAX_MB, beats=adclone.BEATS, social=adclone.social_on())


def _start(conn, p):
    if p is None:
        flash("Add your product first: the remake is made for it.")
        return redirect(url_for("clone.page"))
    f = request.files.get("file")
    link = (request.form.get("url") or "").strip()
    cid = conn.execute("INSERT INTO ad_clones (workspace_id, user_id, product_id, source, status, created_at) "
                       "VALUES (?,?,?,?,?,?)", (g.ws["id"], g.user["id"], p["id"], "", "pending", db.now())).lastrowid
    conn.commit()
    here = adclone.folder(g.ws["id"])
    try:
        if f and f.filename:
            from core import save_upload
            path, ctype = save_upload(f, here, adclone.VIDEO_TYPES | adclone.IMAGE_TYPES, adclone.MAX_MB, ws=g.ws)
            source = f.filename[:200]
        elif link and _social(link) and adclone.social_on():
            # fetched in the run, by NeuraCut (the Mac from home, or the server through its proxy): adclone.fetch_social
            if not URL_RE.match(link) or not adclone.social_host(link):
                raise ValueError("That kind of link can't be read yet. TikTok, Instagram, YouTube, Facebook and X "
                                 "posts work, and Meta Ad Library ads. Or save the video and upload it.")
            path, ctype, source = None, "video/mp4", link[:500]
        elif link:
            path, ctype = _download(link, here)
            source = link[:500]
        else:
            raise ValueError("Upload the ad, or paste a link to the video or image file.")
    except ValueError as e:
        conn.execute("DELETE FROM ad_clones WHERE id=?", (cid,))
        conn.commit()
        flash(str(e))
        return redirect(url_for("clone.page"))
    media = "image" if ctype in adclone.IMAGE_TYPES else "video"
    hold = reserve_ai(adclone.quote(), adclone.LABEL, ref=f"clone:{cid}", conn=conn)
    if hold is None:
        conn.execute("DELETE FROM ad_clones WHERE id=?", (cid,))
        conn.commit()
        if path:
            _remove(path)
        flash(f"Reading an ad takes about {adclone.quote():,} credits and you have {db.balance(g.ws):,}.")
        return redirect(url_for("clone.page"))
    conn.execute("UPDATE ad_clones SET file_path=?, media=?, source=?, hold_id=? WHERE id=?",
                 (path, media, source, getattr(hold, "id", None), cid))
    conn.commit()
    adclone.start(cid)
    return redirect(url_for("clone.view", cid=cid))


def _download(link: str, here: str) -> tuple[str, str]:
    """A link straight to a video or image file (checked like every member link: fetcher.py)."""
    import fetcher
    if not URL_RE.match(link):
        raise ValueError("Paste a link that starts with https://.")
    host = re.sub(r"^https?://(www\.)?", "", link.lower()).split("/")[0]
    if any(host == s or host.endswith("." + s) for s in SOCIAL):
        raise ValueError("Links to social posts can't be read just now. Save the video to your device and upload it.")
    try:
        with fetcher.budget(90):
            r = fetcher.fetch_bytes(link, max_bytes=adclone.MAX_MB * 1024 * 1024, accept="video/*,image/*")
    except fetcher.FetchError as e:
        raise ValueError(str(e)) from None
    ctype = (r["content_type"] or "").split(";")[0].strip().lower() or (mimetypes.guess_type(link)[0] or "")
    if ctype not in adclone.VIDEO_TYPES | adclone.IMAGE_TYPES or r["status"] != 200:
        raise ValueError("That link isn't a video or image file. Upload the ad instead.")
    import storage
    storage.check_upload(g.ws)
    os.makedirs(here, exist_ok=True)
    path = os.path.join(here, f"{os.urandom(12).hex()}{mimetypes.guess_extension(ctype) or ''}")
    with open(path, "wb") as fh:
        fh.write(r["data"])
    storage.check_saved(g.ws, path)
    return path, ctype


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


@bp.route("/app/clone/<int:cid>")
@login_required
def view(cid):
    with db.connect() as conn:
        adclone.ensure_table(conn)
        row = _row(conn, cid)
        p = products.get_product(conn, g.ws["id"], row["product_id"]) if row["product_id"] else None
    v = adclone.view(row)
    return render_template("v2/clone_view.html", c=v, p=p, beats=adclone.BEATS)


@bp.route("/app/clone/<int:cid>/f/<name>")
@login_required
def frame(cid, name):
    if not re.fullmatch(r"f\d{1,2}\.jpg", name):
        abort(404)
    with db.connect() as conn:
        _row(conn, cid)
    return send_from_directory(os.path.join(adclone.folder(g.ws["id"]), f"c{cid}"), name, max_age=3600)


@bp.route("/app/clone/<int:cid>/desk", methods=["POST"])
@login_required
def to_desk(cid):
    """The remake opens as a campaign on the desk, priced row by row; nothing is made until it is run there."""
    import desk
    import studio_v2
    with db.connect() as conn:
        row = _row(conn, cid)
        if row["thread_id"]:
            return redirect(url_for("desk.thread", tid=row["thread_id"]))
        v = adclone.view(row)
        p = products.get_product(conn, g.ws["id"], row["product_id"]) if row["product_id"] else None
        if p is None or v["status"] != "done":
            flash("This ad isn't ready to remake yet.")
            return redirect(url_for("clone.view", cid=cid))
        items = v["remake"].get("items") or []
        cast = studio_v2.roster()
        have = {m["id"] for m in studio_v2.available_modes()}
        raw = [dict(it, kind=it["make"], on=True) for it in items] + [{"kind": k, "on": False} for k in desk.SPINE]
        for i, r in enumerate(raw):
            r["id"] = f"p{i + 1}"
        parts = desk.clean_parts(raw, cast, have)
        ans = desk.price(parts, p, cast)
        order = " → ".join(b["beat"] for b in v["analysis"].get("beats", []))
        said = f"Clone this ad for {p['name']} ({order})."
        note = (v["remake"].get("note") or "Here is the same structure, written for your product.") + \
            " Every part follows the reference's beats with your own words; nothing from the reference is reused."
        tid = desk._new_thread(conn, p, said)
        desk._turn(conn, tid, "member", "said", {"text": said})
        turn = desk._turn(conn, tid, "desk", "plan", {"ask": said, "note": note, "skipped": v["remake"].get("dropped") or [],
                                                     "also": [], "parts": desk._stored(ans["parts"]),
                                                     "rows": desk._snapshot(ans["parts"]), "ran": False})
        conn.execute("UPDATE ad_clones SET thread_id=? WHERE id=?", (tid, cid))
        conn.commit()
    return redirect(url_for("desk.thread", tid=tid) + f"#t{turn}")

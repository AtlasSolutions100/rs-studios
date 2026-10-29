"""Landing pages (/app/pages, public at /p/<slug>) and smart links
(/app/links, public at /l/<code>)."""
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import (Blueprint, abort, flash, g, make_response, redirect, render_template, request, send_file,
                   url_for)

import compliance
import copywriter
import db
import llm
import pages
import plans
from core import brand, can_afford, charge_ai, feature_required, not_enough, picked_product

bp = Blueprint("pages", __name__)
MAX_DRAFTS = 50
LINK_CODE_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")
BOT_RX = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|embedly|whatsapp|telegram|discord|curl|"
                    r"python-requests|wget|headless", re.I)
PASS_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "ttclid",
               "msclkid", "twclid", "li_fat_id", "rdt_cid", "ScCid", "epik")
CLICKS_PER_MIN_PER_IP = 120


def client_ip():
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------- pages (workspace)

def _page(pid):
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM pages WHERE id=? AND workspace_id=?", (pid, g.ws["id"])).fetchone()
    if not p:
        abort(404)
    return p


def _library(conn):
    return conn.execute("SELECT o.id, o.kind, j.template_id, j.tool, j.created_at FROM outputs o JOIN jobs j ON j.id=o.job_id "
                        "WHERE j.workspace_id=? AND j.status='done' ORDER BY o.id DESC LIMIT 60", (g.ws["id"],)).fetchall()


def _media_ok_for(ws_id):
    def ok(oid):
        with db.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND j.workspace_id=?",
                                     (oid, ws_id)).fetchone())
    return ok


def _published_count(conn, exclude=None):
    return conn.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=? AND status='published' AND id!=?",
                        (g.ws["id"], exclude or 0)).fetchone()[0]


def _unique_slug(conn, base):
    base = pages.slugify(base) or "page"
    if len(base) < 3:
        base = (base + "-page")[:60]
    if base in pages.RESERVED:
        base += "-page"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM pages WHERE slug=?", (slug,)).fetchone():
        slug = f"{base[:55]}-{n}"
        n += 1
    return slug


@bp.route("/app/pages", methods=["GET", "POST"])
@feature_required("pages")
def index():
    pid = picked_product() or g.ws["product_id"]
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:120] or (brand(pid).get("product") or "My page")
        with db.connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0] >= MAX_DRAFTS:
                flash(f"You have {MAX_DRAFTS} pages. Delete one to make another.")
                return redirect(url_for("pages.index"))
            slug = _unique_slug(conn, title)
            pid = conn.execute("INSERT INTO pages (workspace_id, slug, title, description, blocks_json, created_at, updated_at) "
                               "VALUES (?,?,?,?,?,?,?)", (g.ws["id"], slug, title, "", json.dumps(pages.starter_blocks(brand(pid))),
                                                          db.now(), db.now())).lastrowid
            conn.commit()
        return redirect(url_for("pages.edit", pid=pid))
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM pages WHERE workspace_id=? ORDER BY id DESC", (g.ws["id"],)).fetchall()
        published = _published_count(conn)
    return render_template("pages.html", rows=rows, published=published, cap=plans.limit(g.ws, "pages"), pid=pid,
                           next_plan=plans.next_plan_for("pages", published))


def _form_ok_for(ws_id):
    def ok(fid):
        if not plans.has(g.ws, "forms"):
            return False
        with db.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM forms WHERE id=? AND workspace_id=? AND archived=0", (fid, ws_id)).fetchone())
    return ok


def _blocks_from_form(f, media_ok):
    blocks = []
    i = 0
    form_ok = _form_ok_for(g.ws["id"])
    while f.get(f"b{i}_type") is not None and i < pages.MAX_BLOCKS:
        kind = f.get(f"b{i}_type")
        data = {k[len(f"b{i}_"):]: v for k, v in f.items() if k.startswith(f"b{i}_")}
        b = pages.clean_block(kind, data, media_ok, form_ok)
        if b:
            blocks.append(b)
        i += 1
    return blocks


@bp.route("/app/pages/<int:pid>", methods=["GET", "POST"])
@feature_required("pages")
def edit(pid):
    p = _page(pid)
    if request.method == "GET":
        with db.connect() as conn:
            lib = _library(conn)
            published = _published_count(conn, exclude=pid)
            myforms = conn.execute("SELECT id, name FROM forms WHERE workspace_id=? AND archived=0 ORDER BY id DESC",
                                   (g.ws["id"],)).fetchall()
        return render_template("page_edit.html", p=p, blocks=json.loads(p["blocks_json"] or "[]"), lib=lib, myforms=myforms,
                               BLOCKS=pages.BLOCKS, can_publish=plans.within(g.ws, "pages", published),
                               next_plan=plans.next_plan_for("pages", published), ai_on=llm.configured(),
                               ai_quote=copywriter.estimate(1, "landing"), base=request.host_url)
    f = request.form
    action = f.get("action") or "save"
    blocks = _blocks_from_form(f, _media_ok_for(g.ws["id"]))
    if action.startswith("add:") and action[4:] in pages.BLOCKS and len(blocks) < pages.MAX_BLOCKS and \
            plans.has(g.ws, pages.BLOCKS[action[4:]].get("feature") or "pages"):
        blocks.append(pages.clean_block(action[4:], {}, lambda _: False))
    elif action.startswith(("up:", "down:", "del:")):
        try:
            i = int(action.split(":")[1])
        except ValueError:
            i = -1
        if 0 <= i < len(blocks):
            if action.startswith("del:"):
                blocks.pop(i)
            elif action.startswith("up:") and i > 0:
                blocks[i - 1], blocks[i] = blocks[i], blocks[i - 1]
            elif action.startswith("down:") and i < len(blocks) - 1:
                blocks[i + 1], blocks[i] = blocks[i], blocks[i + 1]
    title = (f.get("title") or p["title"]).strip()[:120] or p["title"]
    desc = (f.get("description") or "").strip()[:300]
    theme = "dark" if f.get("theme") == "dark" else "light"
    accent = f.get("accent") if pages.HEX_RX.match(f.get("accent") or "") else p["accent"]
    slug = (f.get("slug") or p["slug"]).strip().lower()
    status = p["status"]
    with db.connect() as conn:
        if slug != p["slug"]:
            if not pages.slug_ok(slug):
                flash("Page addresses use 3-60 lowercase letters, numbers and dashes.")
                slug = p["slug"]
            elif conn.execute("SELECT 1 FROM pages WHERE slug=? AND id!=?", (slug, pid)).fetchone():
                flash("That address is taken. Try another.")
                slug = p["slug"]
        chk = compliance.check(title, desc, pages.page_text(blocks))
        if action == "publish":
            if not chk["ok"]:
                for m in chk["blocks"]:
                    flash("Not published: " + m)
            elif not plans.within(g.ws, "pages", _published_count(conn, exclude=pid)):
                nxt = plans.next_plan_for("pages", _published_count(conn, exclude=pid))
                flash(f"Your plan includes {plans.limit(g.ws, 'pages')} published page. "
                      + (f"{nxt['name']} includes unlimited pages." if nxt else "") + " Unpublish another page first, or upgrade.")
            else:
                status = "published"
                flash(f"Published at {request.host_url}p/{slug}")
        elif action == "unpublish":
            status = "draft"
            flash("Unpublished. The page is offline.")
        elif not chk["ok"]:
            for m in chk["blocks"]:
                flash("Heads up, this would block publishing: " + m)
        if status == "published" and not chk["ok"]:
            status = "draft"
            flash("Taken offline until the flagged text is fixed.")
        conn.execute("UPDATE pages SET title=?, description=?, theme=?, accent=?, slug=?, blocks_json=?, status=?, "
                     "updated_at=?, published_at=COALESCE(published_at, CASE WHEN ?='published' THEN ? END) WHERE id=?",
                     (title, desc, theme, accent, slug, json.dumps(blocks), status, db.now(), status, db.now(), pid))
        conn.commit()
    anchor = ""
    if action.startswith(("add:", "up:", "down:")):
        anchor = f"#b{len(blocks) - 1}" if action.startswith("add:") else ""
    if action == "save":
        flash("Saved.", "ok")
    return redirect(url_for("pages.edit", pid=pid) + anchor)


@bp.route("/app/pages/<int:pid>/ai", methods=["POST"])
@feature_required("pages")
def ai_draft(pid):
    """Fill hero, benefits, FAQ and CTA with landing copy from the copy studio."""
    p = _page(pid)
    if not (plans.has(g.ws, "copy") and llm.configured()):
        flash("AI writing isn't switched on yet.")
        return redirect(url_for("pages.edit", pid=pid))
    quote = copywriter.estimate(1, "landing")
    if not can_afford(quote):
        not_enough(quote)
        return redirect(url_for("pages.edit", pid=pid))
    brief = {"offer": (request.form.get("offer") or p["title"])[:300], "goal": "sales",
             "notes": (request.form.get("notes") or "")[:500]}
    if not compliance.check(brief["offer"], brief["notes"])["ok"]:
        flash("That brief breaks the content rules.")
        return redirect(url_for("pages.edit", pid=pid))
    usage = llm.Usage()
    try:
        v = copywriter.write(brand(picked_product()), "landing", "landing", brief, 1, usage)
    except llm.LLMError as e:
        flash(f"The writer didn't finish ({e}).")
        v = []
    with db.connect() as conn:
        if usage.calls:
            charge_ai(conn, usage.credits, "AI landing page copy")
            conn.commit()
    if not v:
        return redirect(url_for("pages.edit", pid=pid))
    fields = {f["name"].lower(): f["text"] for f in v[0]["fields"]}
    blocks = json.loads(p["blocks_json"] or "[]")
    hero = next((b for b in blocks if b["type"] == "hero"), None)
    if hero is None:
        hero = pages.clean_block("hero", {}, lambda _: False)
        blocks.insert(0, hero)
    hero["headline"] = fields.get("headline", hero.get("headline", ""))[:120]
    hero["subhead"] = fields.get("subheadline", hero.get("subhead", ""))[:300]
    hero["button_text"] = fields.get("call to action", hero.get("button_text", ""))[:40]
    for kind, key, src in (("benefits", "items", "benefits"), ("faq", "items", "faq")):
        b = next((x for x in blocks if x["type"] == kind), None)
        if b is not None and fields.get(src):
            b[key] = _faq_to_block(fields[src]) if kind == "faq" else fields[src][:1500]
    cta = next((b for b in blocks if b["type"] == "cta"), None)
    if cta is not None and fields.get("call to action"):
        cta["button_text"] = fields["call to action"][:40]
    with db.connect() as conn:
        conn.execute("UPDATE pages SET blocks_json=?, updated_at=? WHERE id=?", (json.dumps(blocks), db.now(), pid))
        conn.commit()
    flash("Drafted the copy. Check every line before you publish." + ("" if v[0]["ok"] else
                                                                     " Some of it was flagged by the compliance checks."))
    return redirect(url_for("pages.edit", pid=pid))


def _faq_to_block(text):
    """Turn 'Q? A.' lines from the writer into the FAQ block's format."""
    out, lines = [], [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        m = re.match(r"^(?:Q[:.]\s*)?(.+?\?)\s*(?:A[:.]\s*)?(.*)$", ln)
        if m and m.group(2):
            out.append(f"{m.group(1)}\n{m.group(2)}")
        elif m:
            out.append(m.group(1))
        elif out:
            out[-1] += "\n" + re.sub(r"^A[:.]\s*", "", ln)
    return "\n\n".join(out)[:4000]


@bp.route("/app/pages/<int:pid>/preview")
@feature_required("pages")
def preview(pid):
    p = _page(pid)
    return _render_public(p, preview=True)


@bp.route("/app/pages/<int:pid>/delete", methods=["POST"])
@feature_required("pages")
def delete(pid):
    _page(pid)
    with db.connect() as conn:
        conn.execute("DELETE FROM pages WHERE id=? AND workspace_id=?", (pid, g.ws["id"]))
        conn.commit()
    flash("Page deleted.")
    return redirect(url_for("pages.index"))


# ---------------------------------------------------------------- public pages

def _render_public(p, preview=False):
    blocks = json.loads(p["blocks_json"] or "[]")
    if preview:
        media_url = lambda oid: url_for("media", oid=oid)  # noqa: E731
    else:
        media_url = lambda oid: url_for("pages.public_media", slug=p["slug"], oid=oid)  # noqa: E731
    with db.connect() as conn:
        kinds = {}
        ids = list(pages.media_ids(blocks))
        if ids:
            for r in conn.execute(f"SELECT o.id, o.kind FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE j.workspace_id=? "
                                  f"AND o.id IN ({','.join('?' * len(ids))})", (p["workspace_id"], *ids)):
                kinds[r["id"]] = r["kind"]
        forms = _page_forms(conn, p, blocks)
    import forms as formsmod
    resp = make_response(render_template("page_public.html", p=p, blocks=blocks, media_url=media_url, kinds=kinds,
                                         faq_items=pages.faq_items, lines=pages.lines, preview=preview, forms=forms,
                                         form_css=formsmod.logic_css))
    resp.headers["Content-Security-Policy"] = pages.CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if preview:
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _page_forms(conn, p, blocks):
    """Forms used by form blocks on this page: {id: (row, fields, stamp)}."""
    import forms as formsmod
    ids = list(pages.form_ids(blocks))
    if not ids:
        return {}
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (p["workspace_id"],)).fetchone()
    if not plans.has(ws, "forms"):
        return {}
    out = {}
    from flask import current_app
    for f in conn.execute(f"SELECT * FROM forms WHERE workspace_id=? AND archived=0 AND id IN ({','.join('?' * len(ids))})",
                          (p["workspace_id"], *ids)):
        out[f["id"]] = (f, json.loads(f["fields_json"] or "[]"), formsmod.stamp(current_app.secret_key, f["code"]))
    return out


@bp.route("/p/<slug>")
def public(slug):
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM pages WHERE slug=? AND status='published'", (slug.lower(),)).fetchone()
        if not p:
            abort(404)
        key = f"pv:{p['id']}:{client_ip()}"
        if not BOT_RX.search(request.headers.get("User-Agent", "")) and not db.throttled(key, 1, 1800):
            conn.execute("UPDATE pages SET views=views+1 WHERE id=?", (p["id"],))
            conn.commit()
            db.hit(key)
    return _render_public(p)


@bp.route("/p/<slug>/m/<int:oid>")
def public_media(slug, oid):
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM pages WHERE slug=? AND status='published'", (slug.lower(),)).fetchone()
        if not p or oid not in pages.media_ids(json.loads(p["blocks_json"] or "[]")):
            abort(404)
        o = conn.execute("SELECT o.* FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND j.workspace_id=?",
                         (oid, p["workspace_id"])).fetchone()
    if not o:
        abort(404)
    if request.args.get("poster") == "1":
        path = (o["path"] or "") + ".jpg"
    else:
        path = o["path"]
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, conditional=True, max_age=3600)


# ---------------------------------------------------------------- smart links

def _link(lid):
    with db.connect() as conn:
        lk = conn.execute("SELECT * FROM links WHERE id=? AND workspace_id=?", (lid, g.ws["id"])).fetchone()
    if not lk:
        abort(404)
    return lk


@bp.route("/app/links", methods=["GET", "POST"])
@feature_required("links")
def links():
    if request.method == "POST":
        f = request.form
        target = pages.safe_url(f.get("target_url") or "")
        if not target or not target.startswith(("http://", "https://")):
            flash("Add the full link people should land on (https://…).")
            return redirect(url_for("pages.links"))
        if urlsplit(target).hostname == request.host.split(":")[0] and urlsplit(target).path.startswith("/l/"):
            flash("A smart link can't point to another smart link.")
            return redirect(url_for("pages.links"))
        code = (f.get("code") or "").strip()
        with db.connect() as conn:
            if code:
                if not LINK_CODE_RX.match(code) or code.lower() in pages.RESERVED:
                    flash("Custom codes use 3-40 letters, numbers, dashes or underscores.")
                    return redirect(url_for("pages.links"))
                if conn.execute("SELECT 1 FROM links WHERE code=?", (code,)).fetchone():
                    flash("That code is taken.")
                    return redirect(url_for("pages.links"))
            else:
                code = secrets.token_urlsafe(5).replace("-", "x").replace("_", "y")
                while conn.execute("SELECT 1 FROM links WHERE code=?", (code,)).fetchone():
                    code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
            utm = {k: re.sub(r"[^\w.\-+ ]", "", (f.get(k) or "").strip())[:80] for k in ("utm_source", "utm_medium", "utm_campaign")}
            conn.execute("INSERT INTO links (workspace_id, code, title, target_url, utm_source, utm_medium, utm_campaign, "
                         "passthrough, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (g.ws["id"], code, (f.get("title") or "").strip()[:120], target[:1000], utm["utm_source"] or None,
                          utm["utm_medium"] or None, utm["utm_campaign"] or None, 0 if f.get("passthrough") == "0" else 1,
                          db.now()))
            conn.commit()
        flash(f"Smart link ready: {request.host_url}l/{code}")
        return redirect(url_for("pages.links"))
    since = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        rows = conn.execute("SELECT l.*, (SELECT COUNT(*) FROM link_clicks c WHERE c.link_id=l.id AND c.at>?) week "
                            "FROM links l WHERE l.workspace_id=? AND l.archived=0 ORDER BY l.id DESC", (since, g.ws["id"])).fetchall()
        pgs = conn.execute("SELECT slug, title FROM pages WHERE workspace_id=? AND status='published'", (g.ws["id"],)).fetchall()
    return render_template("links.html", rows=rows, base=request.host_url, pgs=pgs,
                           prefill=request.args.get("target", "")[:500])


@bp.route("/app/links/<int:lid>")
@feature_required("links")
def link_detail(lid):
    lk = _link(lid)
    full = plans.has(g.ws, "link_analytics")
    now = datetime.now(timezone.utc)
    since30 = (now - timedelta(days=30)).replace(microsecond=0).isoformat()
    since7 = (now - timedelta(days=7)).replace(microsecond=0).isoformat()
    stats = {}
    with db.connect() as conn:
        q = lambda sql, *a: conn.execute(sql, (lid, *a)).fetchall()  # noqa: E731
        stats["week"] = q("SELECT COUNT(*) n FROM link_clicks WHERE link_id=? AND at>?", since7)[0]["n"]
        if full:
            days = {r["d"]: r["n"] for r in q("SELECT substr(at,1,10) d, COUNT(*) n FROM link_clicks WHERE link_id=? AND at>? "
                                              "GROUP BY d", since30)}
            stats["days"] = [((now - timedelta(days=29 - i)).strftime("%Y-%m-%d"),
                              days.get((now - timedelta(days=29 - i)).strftime("%Y-%m-%d"), 0)) for i in range(30)]
            for col in ("referrer", "country", "device", "utm_source", "utm_medium", "utm_campaign"):
                stats[col] = q(f"SELECT COALESCE(NULLIF({col}, ''), '(none)') k, COUNT(*) n FROM link_clicks WHERE link_id=? "
                               f"AND at>? GROUP BY k ORDER BY n DESC LIMIT 8", since30)
    return render_template("link_detail.html", lk=lk, stats=stats, full=full, base=request.host_url,
                           dest=_destination(lk, {}))


@bp.route("/app/links/<int:lid>/archive", methods=["POST"])
@feature_required("links")
def link_archive(lid):
    _link(lid)
    with db.connect() as conn:
        conn.execute("UPDATE links SET archived=1 WHERE id=? AND workspace_id=?", (lid, g.ws["id"]))
        conn.commit()
    flash("Link archived. It no longer redirects.")
    return redirect(url_for("pages.links"))


def _destination(lk, incoming: dict) -> str:
    parts = urlsplit(lk["target_url"])
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if parts.path.startswith("/p/") and request and parts.hostname == request.host.split(":")[0]:
        q["cfy_l"] = lk["code"]   # our own page: its forms can credit this link
    for k in ("utm_source", "utm_medium", "utm_campaign"):
        if lk[k] and k not in q:
            q[k] = lk[k]
    if lk["passthrough"]:
        for k in PASS_PARAMS:
            v = incoming.get(k)
            if v:
                q[k] = str(v)[:200]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


@bp.route("/l/<code>")
def go(code):
    ip = client_ip()
    if db.throttled(f"l:{ip}", CLICKS_PER_MIN_PER_IP, 60):
        return "Too many requests. Try again in a minute.", 429
    with db.connect() as conn:
        lk = conn.execute("SELECT * FROM links WHERE code=? AND archived=0", (code,)).fetchone()
        if not lk:
            abort(404)
        ua = request.headers.get("User-Agent", "")
        if not BOT_RX.search(ua):
            ref = urlsplit(request.referrer or "").hostname or ""
            country = (request.headers.get("CF-IPCountry") or request.headers.get("X-Country") or "").upper()[:2]
            device = "mobile" if re.search(r"Mobile|Android|iPhone|iPad", ua) else "desktop"
            conn.execute("INSERT INTO link_clicks (link_id, at, referrer, country, device, utm_source, utm_medium, utm_campaign) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (lk["id"], db.now(), ref[:120], country if country.isalpha() else "", device,
                          (request.args.get("utm_source") or "")[:80], (request.args.get("utm_medium") or "")[:80],
                          (request.args.get("utm_campaign") or "")[:80]))
            conn.execute("UPDATE links SET clicks=clicks+1 WHERE id=?", (lk["id"],))
            conn.commit()
    db.hit(f"l:{ip}")
    resp = redirect(_destination(lk, request.args), code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp

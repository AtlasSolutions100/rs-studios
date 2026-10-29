"""Community and courses (Scale).

Workspace side (/app/community): set up the community, spaces, courses
(modules -> lessons with text and video), members (invite links, roles) and
events. Member side (/c/<slug>): members sign in with their own account (not a
workspace seat), read and post in spaces, comment, take courses (progress is
saved), and see events (with an .ics file). Workspace users who own the
community see it as admins automatically.

Member pages run no scripts and send the same strict CSP as landing pages.
Every query is scoped to the community's workspace.
"""
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (Blueprint, Response, abort, flash, g, make_response, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import pages
import plans
from core import feature_required, is_owner

bp = Blueprint("community", __name__)
POSTS_PER_HOUR, COMMENTS_PER_HOUR = 20, 60
TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
             "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "America/Mexico_City", "America/Sao_Paulo",
             "Europe/London", "Europe/Paris", "Europe/Berlin", "Africa/Johannesburg", "Asia/Dubai", "Asia/Kolkata",
             "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney", "UTC"]


def _clip(text, n):
    return (text or "").replace("\r\n", "\n").strip()[:n]


# ---------------------------------------------------------------- shared helpers

def community_for_ws(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM communities WHERE workspace_id=?", (ws_id,)).fetchone()


def _ws(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def _community(slug):
    """The community for a public slug, only while its plan includes it."""
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM communities WHERE slug=?", ((slug or "").lower(),)).fetchone()
    if not c or not plans.has(_ws(c["workspace_id"]), "community"):
        abort(404)
    return c


def viewer(c):
    """The signed-in community member, or None. Workspace users of the owning
    workspace are members automatically (owners as 'owner', others 'admin')."""
    ws_id = c["workspace_id"]
    mid = (session.get("cm") or {}).get(str(ws_id))
    with db.connect() as conn:
        if mid:
            m = conn.execute("SELECT * FROM c_members WHERE id=? AND workspace_id=? AND status='active'", (mid, ws_id)).fetchone()
            if m:
                return m
        if g.user and g.user["workspace_id"] == ws_id:
            m = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND user_id=?", (ws_id, g.user["id"])).fetchone()
            if not m:
                m = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND email=?", (ws_id, g.user["email"])).fetchone()
            role = "owner" if is_owner() else "admin"
            if m:
                conn.execute("UPDATE c_members SET user_id=?, role=?, status='active' WHERE id=?", (g.user["id"], role, m["id"]))
            else:
                conn.execute("INSERT INTO c_members (workspace_id, email, name, role, status, user_id, created_at) "
                             "VALUES (?,?,?,?,?,?,?)", (ws_id, g.user["email"], g.user["name"] or g.user["email"].split("@")[0],
                                                       role, "active", g.user["id"], db.now()))
            conn.commit()
            return conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND user_id=?", (ws_id, g.user["id"])).fetchone()
    return None


def _need_member(c):
    m = viewer(c)
    if not m:
        abort(redirect(url_for("community.login", slug=c["slug"], next=request.path)))
    with db.connect() as conn:
        conn.execute("UPDATE c_members SET last_seen=? WHERE id=?", (db.now(), m["id"]))
        conn.commit()
    return m


def _is_admin(m):
    return bool(m) and m["role"] in ("owner", "admin")


def _render(tpl, c, status=200, **kw):
    resp = make_response(render_template(tpl, c=c, me=viewer(c), is_admin=_is_admin(viewer(c)), **kw), status)
    resp.headers["Content-Security-Policy"] = pages.CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def event_times(e):
    """(local datetime, utc datetime) for an event row."""
    try:
        tz = ZoneInfo(e["tz"] or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    local = datetime.fromisoformat(e["starts_at"]).replace(tzinfo=tz)
    return local, local.astimezone(timezone.utc)


# ---------------------------------------------------------------- workspace admin

def _admin_data(conn, ws_id):
    return {
        "spaces": conn.execute("SELECT s.*, (SELECT COUNT(*) FROM c_posts p WHERE p.space_id=s.id) posts FROM c_spaces s "
                               "WHERE workspace_id=? ORDER BY position, id", (ws_id,)).fetchall(),
        "courses": conn.execute("SELECT c.*, (SELECT COUNT(*) FROM c_lessons l JOIN c_modules m ON m.id=l.module_id "
                                "WHERE m.course_id=c.id) lessons FROM c_courses c WHERE workspace_id=? ORDER BY position, id",
                                (ws_id,)).fetchall(),
        "members": conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND status!='removed' ORDER BY "
                                "CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, id", (ws_id,)).fetchall(),
        "events": conn.execute("SELECT * FROM c_events WHERE workspace_id=? ORDER BY starts_at DESC LIMIT 50", (ws_id,)).fetchall(),
    }


@bp.route("/app/community", methods=["GET", "POST"])
@feature_required("community")
def admin():
    c = community_for_ws(g.ws["id"])
    if request.method == "POST":
        f = request.form
        name = _clip(f.get("name"), 80) or (c["name"] if c else g.ws["name"])
        slug = pages.slugify(f.get("slug") or name)
        with db.connect() as conn:
            taken = conn.execute("SELECT 1 FROM communities WHERE slug=? AND workspace_id!=?", (slug, g.ws["id"])).fetchone()
            if not pages.slug_ok(slug) or taken:
                flash("That address is taken or not allowed. Use 3-60 lowercase letters, numbers and dashes.")
                return redirect(url_for("community.admin"))
            if c:
                conn.execute("UPDATE communities SET name=?, slug=?, about=?, open_join=? WHERE workspace_id=?",
                             (name, slug, _clip(f.get("about"), 1000), 1 if f.get("open_join") == "1" else 0, g.ws["id"]))
            else:
                conn.execute("INSERT INTO communities (workspace_id, name, slug, about, join_code, open_join, created_at) "
                             "VALUES (?,?,?,?,?,?,?)", (g.ws["id"], name, slug, _clip(f.get("about"), 1000),
                                                        secrets.token_urlsafe(9), 0, db.now()))
                conn.execute("INSERT INTO c_spaces (workspace_id, name, about, created_at) VALUES (?,?,?,?)",
                             (g.ws["id"], "General", "Say hello and ask anything.", db.now()))
            conn.commit()
        flash("Saved.")
        return redirect(url_for("community.admin"))
    if not c:
        return render_template("community_setup.html")
    with db.connect() as conn:
        data = _admin_data(conn, g.ws["id"])
    return render_template("community_admin.html", c=c, base=request.host_url, TZ=TIMEZONES, **data)


@bp.route("/app/community/<what>", methods=["POST"])
@feature_required("community")
def admin_add(what):
    c = community_for_ws(g.ws["id"]) or abort(404)
    f = request.form
    ws = g.ws["id"]
    with db.connect() as conn:
        if what == "spaces":
            name = _clip(f.get("name"), 60)
            if name:
                conn.execute("INSERT INTO c_spaces (workspace_id, name, about, created_at) VALUES (?,?,?,?)",
                             (ws, name, _clip(f.get("about"), 300), db.now()))
        elif what == "courses":
            title = _clip(f.get("title"), 120)
            if title:
                cid = conn.execute("INSERT INTO c_courses (workspace_id, title, about, created_at) VALUES (?,?,?,?)",
                                   (ws, title, _clip(f.get("about"), 1000), db.now())).lastrowid
                conn.execute("INSERT INTO c_modules (workspace_id, course_id, title) VALUES (?,?,?)", (ws, cid, "Module 1"))
                conn.commit()
                return redirect(url_for("community.course_edit", cid=cid))
        elif what == "events":
            title, tz = _clip(f.get("title"), 120), f.get("tz") if f.get("tz") in TIMEZONES else "UTC"
            try:
                starts = datetime.fromisoformat((f.get("starts_at") or "").strip()).replace(second=0, microsecond=0)
            except ValueError:
                starts = None
            if not title or not starts:
                flash("Give the event a title and a start time.")
            else:
                conn.execute("INSERT INTO c_events (workspace_id, title, starts_at, tz, duration_min, link, about, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (ws, title, starts.isoformat(), tz, max(5, min(600, int(f.get("duration") or 60))),
                              pages.safe_url(f.get("link") or "") or None, _clip(f.get("about"), 1000), db.now()))
        elif what == "members":
            email = _clip(f.get("email"), 200).lower()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
                flash("Add a valid email.")
            else:
                m = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND email=?", (ws, email)).fetchone()
                code = secrets.token_urlsafe(12)
                role = "admin" if f.get("role") == "admin" else "member"
                if m and m["status"] == "active":
                    flash("They're already a member.")
                    return redirect(url_for("community.admin") + "#members")
                if m:
                    conn.execute("UPDATE c_members SET status='invited', invite_code=?, role=? WHERE id=?", (code, role, m["id"]))
                else:
                    conn.execute("INSERT INTO c_members (workspace_id, email, name, role, status, invite_code, created_at) "
                                 "VALUES (?,?,?,?,?,?,?)", (ws, email, _clip(f.get("name"), 80) or None, role, "invited",
                                                            code, db.now()))
                flash(f"Invite link for {email} (send it to them; it works once): "
                      f"{request.host_url}c/{c['slug']}/join/{code}")
        elif what == "rotate":
            conn.execute("UPDATE communities SET join_code=? WHERE workspace_id=?", (secrets.token_urlsafe(9), ws))
            flash("New join link made. The old one stops working.")
        else:
            abort(404)
        conn.commit()
    return redirect(url_for("community.admin") + f"#{what}")


@bp.route("/app/community/<what>/<int:oid>/<action>", methods=["POST"])
@feature_required("community")
def admin_item(what, oid, action):
    ws = g.ws["id"]
    with db.connect() as conn:
        if what == "spaces" and action == "delete":
            conn.execute("DELETE FROM c_comments WHERE workspace_id=? AND post_id IN (SELECT id FROM c_posts WHERE space_id=? "
                         "AND workspace_id=?)", (ws, oid, ws))
            conn.execute("DELETE FROM c_posts WHERE space_id=? AND workspace_id=?", (oid, ws))
            conn.execute("DELETE FROM c_spaces WHERE id=? AND workspace_id=?", (oid, ws))
        elif what == "events" and action == "delete":
            conn.execute("DELETE FROM c_events WHERE id=? AND workspace_id=?", (oid, ws))
        elif what == "courses" and action == "delete":
            mods = [r["id"] for r in conn.execute("SELECT id FROM c_modules WHERE course_id=? AND workspace_id=?", (oid, ws))]
            for mid in mods:
                conn.execute("DELETE FROM c_lessons WHERE module_id=? AND workspace_id=?", (mid, ws))
            conn.execute("DELETE FROM c_modules WHERE course_id=? AND workspace_id=?", (oid, ws))
            conn.execute("DELETE FROM c_courses WHERE id=? AND workspace_id=?", (oid, ws))
        elif what == "members" and action in ("remove", "admin", "member"):
            m = conn.execute("SELECT * FROM c_members WHERE id=? AND workspace_id=?", (oid, ws)).fetchone()
            if not m:
                abort(404)
            if m["role"] == "owner" or m["user_id"]:
                flash("Workspace people manage their access from the Team page.")
            elif action == "remove":
                conn.execute("UPDATE c_members SET status='removed', invite_code=NULL WHERE id=?", (oid,))
            else:
                conn.execute("UPDATE c_members SET role=? WHERE id=?", (action, oid))
        else:
            abort(404)
        conn.commit()
    return redirect(url_for("community.admin") + f"#{what}")


def _course(cid):
    with db.connect() as conn:
        co = conn.execute("SELECT * FROM c_courses WHERE id=? AND workspace_id=?", (cid, g.ws["id"])).fetchone()
    if not co:
        abort(404)
    return co


def _outline(conn, ws, cid):
    mods = conn.execute("SELECT * FROM c_modules WHERE course_id=? AND workspace_id=? ORDER BY position, id", (cid, ws)).fetchall()
    lessons = {}
    for ls in conn.execute("SELECT l.* FROM c_lessons l JOIN c_modules m ON m.id=l.module_id WHERE m.course_id=? "
                           "AND l.workspace_id=? ORDER BY l.position, l.id", (cid, ws)):
        lessons.setdefault(ls["module_id"], []).append(ls)
    return mods, lessons


@bp.route("/app/community/courses/<int:cid>", methods=["GET", "POST"])
@feature_required("community")
def course_edit(cid):
    co = _course(cid)
    ws = g.ws["id"]
    if request.method == "POST":
        f = request.form
        action = f.get("action") or "save"
        with db.connect() as conn:
            if action == "save":
                conn.execute("UPDATE c_courses SET title=?, about=?, published=? WHERE id=?",
                             (_clip(f.get("title"), 120) or co["title"], _clip(f.get("about"), 1000),
                              1 if f.get("published") == "1" else 0, cid))
                for k, v in f.items():
                    m = re.match(r"^mod_(\d+)$", k)
                    if m and v.strip():
                        conn.execute("UPDATE c_modules SET title=? WHERE id=? AND course_id=? AND workspace_id=?",
                                     (_clip(v, 120), int(m.group(1)), cid, ws))
                flash("Saved.")
            elif action == "add_module":
                n = conn.execute("SELECT COUNT(*) FROM c_modules WHERE course_id=?", (cid,)).fetchone()[0]
                conn.execute("INSERT INTO c_modules (workspace_id, course_id, title, position) VALUES (?,?,?,?)",
                             (ws, cid, f"Module {n + 1}", n))
            elif action.startswith("add_lesson:"):
                mid = int(action.split(":")[1])
                if conn.execute("SELECT 1 FROM c_modules WHERE id=? AND course_id=? AND workspace_id=?", (mid, cid, ws)).fetchone():
                    n = conn.execute("SELECT COUNT(*) FROM c_lessons WHERE module_id=?", (mid,)).fetchone()[0]
                    lid = conn.execute("INSERT INTO c_lessons (workspace_id, module_id, title, position) VALUES (?,?,?,?)",
                                       (ws, mid, f"Lesson {n + 1}", n)).lastrowid
                    conn.commit()
                    return redirect(url_for("community.lesson_edit", lid=lid))
            elif action.startswith("del_module:"):
                mid = int(action.split(":")[1])
                conn.execute("DELETE FROM c_lessons WHERE module_id=? AND workspace_id=? AND module_id IN "
                             "(SELECT id FROM c_modules WHERE course_id=?)", (mid, ws, cid))
                conn.execute("DELETE FROM c_modules WHERE id=? AND course_id=? AND workspace_id=?", (mid, cid, ws))
            conn.commit()
        return redirect(url_for("community.course_edit", cid=cid))
    with db.connect() as conn:
        mods, lessons = _outline(conn, ws, cid)
    return render_template("course_edit.html", co=co, mods=mods, lessons=lessons, c=community_for_ws(ws))


@bp.route("/app/community/lessons/<int:lid>", methods=["GET", "POST"])
@feature_required("community")
def lesson_edit(lid):
    ws = g.ws["id"]
    with db.connect() as conn:
        ls = conn.execute("SELECT l.*, m.course_id FROM c_lessons l JOIN c_modules m ON m.id=l.module_id "
                          "WHERE l.id=? AND l.workspace_id=?", (lid, ws)).fetchone()
        if not ls:
            abort(404)
        if request.method == "POST":
            f = request.form
            if f.get("action") == "delete":
                conn.execute("DELETE FROM c_lessons WHERE id=?", (lid,))
                conn.commit()
                return redirect(url_for("community.course_edit", cid=ls["course_id"]))
            media = f.get("media_id", type=int)
            if media and not conn.execute("SELECT 1 FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND j.workspace_id=?",
                                          (media, ws)).fetchone():
                media = None
            embed = pages.safe_url(f.get("embed_url") or "")
            if embed and not pages.embed_src(embed):
                flash("That video link can't be embedded. Use YouTube, Vimeo or Loom.")
                embed = ""
            conn.execute("UPDATE c_lessons SET title=?, body=?, media_id=?, embed_url=? WHERE id=?",
                         (_clip(f.get("title"), 120) or ls["title"], _clip(f.get("body"), 20000), media, embed or None, lid))
            conn.commit()
            flash("Lesson saved.")
            return redirect(url_for("community.lesson_edit", lid=lid))
        lib = conn.execute("SELECT o.id, o.kind FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE j.workspace_id=? "
                           "AND j.status='done' ORDER BY o.id DESC LIMIT 60", (ws,)).fetchall()
    return render_template("lesson_edit.html", ls=ls, lib=lib)


# ---------------------------------------------------------------- member site

@bp.route("/c/<slug>")
def home(slug):
    c = _community(slug)
    me = viewer(c)
    if not me:
        return _render("c_welcome.html", c)
    _need_member(c)
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        spaces = conn.execute("SELECT s.*, (SELECT COUNT(*) FROM c_posts p WHERE p.space_id=s.id) posts FROM c_spaces s "
                              "WHERE workspace_id=? ORDER BY position, id", (c["workspace_id"],)).fetchall()
        courses = conn.execute("SELECT * FROM c_courses WHERE workspace_id=? AND published=1 ORDER BY position, id",
                               (c["workspace_id"],)).fetchall()
        events = [e for e in conn.execute("SELECT * FROM c_events WHERE workspace_id=? ORDER BY starts_at",
                                          (c["workspace_id"],)).fetchall()
                  if event_times(e)[1] + timedelta(minutes=e["duration_min"]) > now][:10]
        recent = conn.execute("SELECT p.*, m.name author, s.name space FROM c_posts p JOIN c_members m ON m.id=p.member_id "
                              "JOIN c_spaces s ON s.id=p.space_id WHERE p.workspace_id=? ORDER BY p.id DESC LIMIT 8",
                              (c["workspace_id"],)).fetchall()
    return _render("c_home.html", c, spaces=spaces, courses=courses, events=events, recent=recent, event_times=event_times)


@bp.route("/c/<slug>/login", methods=["GET", "POST"])
def login(slug):
    c = _community(slug)
    if request.method == "POST":
        email = _clip(request.form.get("email"), 200).lower()
        ip = request.remote_addr or "unknown"
        key = f"clogin:{c['workspace_id']}:{ip}"
        if db.throttled(key, 10, 900):
            flash("Too many tries. Wait 15 minutes.")
            return _render("c_login.html", c, 429)
        with db.connect() as conn:
            m = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND email=? AND status='active'",
                             (c["workspace_id"], email)).fetchone()
        if m and m["pw_hash"] and check_password_hash(m["pw_hash"], request.form.get("password") or ""):
            cm = dict(session.get("cm") or {})
            cm[str(c["workspace_id"])] = m["id"]
            session["cm"] = cm
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith(f"/c/{c['slug']}") else url_for("community.home", slug=c["slug"]))
        db.hit(key)
        flash("That email and password don't match.")
    return _render("c_login.html", c)


@bp.route("/c/<slug>/logout", methods=["POST"])
def logout(slug):
    c = _community(slug)
    cm = dict(session.get("cm") or {})
    cm.pop(str(c["workspace_id"]), None)
    session["cm"] = cm
    return redirect(url_for("community.home", slug=c["slug"]))


@bp.route("/c/<slug>/join/<code>", methods=["GET", "POST"])
def join(slug, code):
    c = _community(slug)
    with db.connect() as conn:
        invite = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND invite_code=? AND status='invited'",
                              (c["workspace_id"], code)).fetchone()
    open_link = c["open_join"] and secrets.compare_digest(code, c["join_code"])
    if not invite and not open_link:
        return _render("c_join.html", c, 404, bad=True, invite=None)
    if request.method == "POST":
        f = request.form
        ip = request.remote_addr or "unknown"
        if f.get("website") or db.throttled(f"cjoin:{ip}", 10, 3600):
            return _render("c_join.html", c, 429, bad=False, invite=invite, error="Too many sign-ups. Try again later.")
        email = invite["email"] if invite else _clip(f.get("email"), 200).lower()
        name, pw = _clip(f.get("name"), 80), f.get("password") or ""
        if not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email) or len(pw) < 8 or not name:
            return _render("c_join.html", c, 400, bad=False, invite=invite,
                           error="Add your name, a real email and a password of at least 8 characters.")
        db.hit(f"cjoin:{ip}")
        with db.connect() as conn:
            if invite:
                conn.execute("UPDATE c_members SET name=?, pw_hash=?, status='active', invite_code=NULL WHERE id=?",
                             (name, generate_password_hash(pw), invite["id"]))
                mid = invite["id"]
            else:
                m = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND email=?", (c["workspace_id"], email)).fetchone()
                if m and m["status"] == "active":
                    return _render("c_join.html", c, 400, bad=False, invite=None,
                                   error="That email is already a member. Log in instead.")
                if m and m["status"] == "removed":
                    return _render("c_join.html", c, 403, bad=False, invite=None, error="This email can't join.")
                if m:
                    conn.execute("UPDATE c_members SET name=?, pw_hash=?, status='active', invite_code=NULL WHERE id=?",
                                 (name, generate_password_hash(pw), m["id"]))
                    mid = m["id"]
                else:
                    mid = conn.execute("INSERT INTO c_members (workspace_id, email, name, pw_hash, role, status, created_at) "
                                       "VALUES (?,?,?,?,?,?,?)", (c["workspace_id"], email, name, generate_password_hash(pw),
                                                                  "member", "active", db.now())).lastrowid
            conn.commit()
        cm = dict(session.get("cm") or {})
        cm[str(c["workspace_id"])] = mid
        session["cm"] = cm
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return redirect(url_for("community.home", slug=c["slug"]))
    return _render("c_join.html", c, bad=False, invite=invite)


def _space(c, sid):
    with db.connect() as conn:
        s = conn.execute("SELECT * FROM c_spaces WHERE id=? AND workspace_id=?", (sid, c["workspace_id"])).fetchone()
    if not s:
        abort(404)
    return s


@bp.route("/c/<slug>/s/<int:sid>", methods=["GET", "POST"])
def space(slug, sid):
    c = _community(slug)
    me = _need_member(c)
    s = _space(c, sid)
    if request.method == "POST":
        title, body = _clip(request.form.get("title"), 160), _clip(request.form.get("body"), 8000)
        key = f"cpost:{me['id']}"
        if not title:
            flash("Give your post a title.")
        elif db.throttled(key, POSTS_PER_HOUR, 3600):
            flash("That's a lot of posts this hour. Try again later.")
        else:
            db.hit(key)
            with db.connect() as conn:
                pid = conn.execute("INSERT INTO c_posts (workspace_id, space_id, member_id, title, body, created_at) "
                                   "VALUES (?,?,?,?,?,?)", (c["workspace_id"], sid, me["id"], title, body, db.now())).lastrowid
                conn.commit()
            return redirect(url_for("community.post", slug=slug, pid=pid))
        return redirect(url_for("community.space", slug=slug, sid=sid))
    with db.connect() as conn:
        posts = conn.execute("SELECT p.*, m.name author, (SELECT COUNT(*) FROM c_comments x WHERE x.post_id=p.id) comments "
                             "FROM c_posts p JOIN c_members m ON m.id=p.member_id WHERE p.space_id=? AND p.workspace_id=? "
                             "ORDER BY p.pinned DESC, p.id DESC LIMIT 100", (sid, c["workspace_id"])).fetchall()
    return _render("c_space.html", c, s=s, posts=posts)


@bp.route("/c/<slug>/posts/<int:pid>", methods=["GET", "POST"])
def post(slug, pid):
    c = _community(slug)
    me = _need_member(c)
    with db.connect() as conn:
        p = conn.execute("SELECT p.*, m.name author FROM c_posts p JOIN c_members m ON m.id=p.member_id "
                         "WHERE p.id=? AND p.workspace_id=?", (pid, c["workspace_id"])).fetchone()
        if not p:
            abort(404)
        if request.method == "POST":
            action = request.form.get("action") or "comment"
            if action == "comment":
                body = _clip(request.form.get("body"), 4000)
                key = f"ccom:{me['id']}"
                if body and not db.throttled(key, COMMENTS_PER_HOUR, 3600):
                    db.hit(key)
                    conn.execute("INSERT INTO c_comments (workspace_id, post_id, member_id, body, created_at) VALUES (?,?,?,?,?)",
                                 (c["workspace_id"], pid, me["id"], body, db.now()))
            elif action == "pin" and _is_admin(me):
                conn.execute("UPDATE c_posts SET pinned=1-pinned WHERE id=?", (pid,))
            elif action == "delete" and (_is_admin(me) or p["member_id"] == me["id"]):
                conn.execute("DELETE FROM c_comments WHERE post_id=?", (pid,))
                conn.execute("DELETE FROM c_posts WHERE id=?", (pid,))
                conn.commit()
                return redirect(url_for("community.space", slug=slug, sid=p["space_id"]))
            elif action.startswith("delc:"):
                cid = int(action[5:]) if action[5:].isdigit() else 0
                cm = conn.execute("SELECT * FROM c_comments WHERE id=? AND post_id=?", (cid, pid)).fetchone()
                if cm and (_is_admin(me) or cm["member_id"] == me["id"]):
                    conn.execute("DELETE FROM c_comments WHERE id=?", (cid,))
            else:
                abort(403)
            conn.commit()
            return redirect(url_for("community.post", slug=slug, pid=pid) + "#comments")
        comments = conn.execute("SELECT x.*, m.name author FROM c_comments x JOIN c_members m ON m.id=x.member_id "
                                "WHERE x.post_id=? ORDER BY x.id", (pid,)).fetchall()
        s = conn.execute("SELECT * FROM c_spaces WHERE id=?", (p["space_id"],)).fetchone()
    return _render("c_post.html", c, p=p, comments=comments, s=s)


@bp.route("/c/<slug>/courses/<int:cid>")
def course(slug, cid):
    c = _community(slug)
    me = _need_member(c)
    with db.connect() as conn:
        co = conn.execute("SELECT * FROM c_courses WHERE id=? AND workspace_id=?", (cid, c["workspace_id"])).fetchone()
        if not co or (not co["published"] and not _is_admin(me)):
            abort(404)
        mods, lessons = _outline(conn, c["workspace_id"], cid)
        done = {r["lesson_id"] for r in conn.execute("SELECT lesson_id FROM c_progress WHERE member_id=?", (me["id"],))}
    total = sum(len(v) for v in lessons.values())
    finished = sum(1 for v in lessons.values() for ls in v if ls["id"] in done)
    return _render("c_course.html", c, co=co, mods=mods, lessons=lessons, done=done, total=total, finished=finished)


@bp.route("/c/<slug>/lessons/<int:lid>", methods=["GET", "POST"])
def lesson(slug, lid):
    c = _community(slug)
    me = _need_member(c)
    with db.connect() as conn:
        ls = conn.execute("SELECT l.*, m.course_id, co.title course_title, co.published FROM c_lessons l "
                          "JOIN c_modules m ON m.id=l.module_id JOIN c_courses co ON co.id=m.course_id "
                          "WHERE l.id=? AND l.workspace_id=?", (lid, c["workspace_id"])).fetchone()
        if not ls or (not ls["published"] and not _is_admin(me)):
            abort(404)
        if request.method == "POST":
            if conn.execute("SELECT 1 FROM c_progress WHERE member_id=? AND lesson_id=?", (me["id"], lid)).fetchone():
                conn.execute("DELETE FROM c_progress WHERE member_id=? AND lesson_id=?", (me["id"], lid))
            else:
                conn.execute("INSERT INTO c_progress (member_id, lesson_id, done_at) VALUES (?,?,?)", (me["id"], lid, db.now()))
            conn.commit()
            return redirect(url_for("community.lesson", slug=slug, lid=lid))
        mods, lessons = _outline(conn, c["workspace_id"], ls["course_id"])
        order = [x for m in mods for x in lessons.get(m["id"], [])]
        done = bool(conn.execute("SELECT 1 FROM c_progress WHERE member_id=? AND lesson_id=?", (me["id"], lid)).fetchone())
        kind = None
        if ls["media_id"]:
            o = conn.execute("SELECT o.kind FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND j.workspace_id=?",
                             (ls["media_id"], c["workspace_id"])).fetchone()
            kind = o["kind"] if o else None
    idx = next((i for i, x in enumerate(order) if x["id"] == lid), 0)
    return _render("c_lesson.html", c, ls=ls, done=done, kind=kind, embed=pages.embed_src(ls["embed_url"] or ""),
                   prev=order[idx - 1] if idx > 0 else None, nxt=order[idx + 1] if idx + 1 < len(order) else None)


@bp.route("/c/<slug>/m/<int:oid>")
def media(slug, oid):
    c = _community(slug)
    _need_member(c)
    with db.connect() as conn:
        ok = conn.execute("SELECT o.* FROM outputs o JOIN jobs j ON j.id=o.job_id JOIN c_lessons l ON l.media_id=o.id "
                          "WHERE o.id=? AND j.workspace_id=? AND l.workspace_id=?", (oid, c["workspace_id"], c["workspace_id"])).fetchone()
    if not ok or not ok["path"] or not os.path.exists(ok["path"]):
        abort(404)
    return send_file(ok["path"], conditional=True)


@bp.route("/c/<slug>/events/<int:eid>.ics")
def event_ics(slug, eid):
    c = _community(slug)
    _need_member(c)
    with db.connect() as conn:
        e = conn.execute("SELECT * FROM c_events WHERE id=? AND workspace_id=?", (eid, c["workspace_id"])).fetchone()
    if not e:
        abort(404)
    _, start = event_times(e)
    end = start + timedelta(minutes=e["duration_min"])
    esc = lambda t: (t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")  # noqa: E731
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Campaignify//Community//EN", "BEGIN:VEVENT",
             f"UID:event-{e['id']}@{request.host}", f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
             f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}", f"SUMMARY:{esc(e['title'])}",
             f"DESCRIPTION:{esc((e['about'] or '') + (chr(10) + e['link'] if e['link'] else ''))}"]
    if e["link"]:
        lines.append(f"URL:{e['link']}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename=event-{e['id']}.ics"})

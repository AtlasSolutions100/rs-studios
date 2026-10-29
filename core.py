"""Helpers every page module shares: access guards, the active brand, and
charging credits for AI calls. Blueprints import from here (never from app.py)."""
import functools
import json

from flask import abort, flash, g, redirect, render_template, request, url_for

import db
import plans


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


def brand():
    w = g.ws
    return {"name": w["brand_name"] or w["name"], "product": w["product_name"] or "",
            "claims": w["claims"] or "", "audience": w["audience"] or ""}


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


def queue_job(conn, tool, tpl_id, brief, params, usd, credits, quiet=False):
    """Insert a pending job and take its credits in one transaction. None (and a
    message) when the workspace can't afford it."""
    now = db.now()
    cur = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, template_id, brief, params_json, "
                       "status, credits, cost_usd, created_at, updated_at, priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (g.ws["id"], g.user["id"], tool, tpl_id, brief, json.dumps(params), "pending",
                        credits, round(usd, 4), now, now, plans.priority(g.ws)))
    jid = cur.lastrowid
    if not db.adjust_credits(conn, g.ws["id"], -credits, "generation", jid):
        conn.rollback()
        if not quiet:
            flash(f"That needs {credits:,} credits and you have {db.balance(g.ws):,}. Buy a credit pack or pick a plan "
                  "on the Billing page.")
        return None
    conn.commit()
    return jid

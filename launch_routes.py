"""The Launch page: a plan made into a PAUSED Meta campaign (roadmap M2.3; launches.py does the work), or a PAUSED
Google Ads campaign, Search or Performance Max (M3.2, M3.3; launch_google.py has Google's draft and card).

    GET  /app/campaigns/<cid>/launch/meta            the spec in plain words (editable) and the approval card
    POST /app/campaigns/<cid>/launch/meta            "Update the card" with the member's edits (any member)
    POST /app/campaigns/<cid>/launch/meta/approve    "Create paused campaign": an owner approves the card shown
    GET  /app/launches/<lid>                         progress, what was created, Ads Manager links, errors
    GET  /app/launches/<lid>.json                    the same, for the page's polling
    POST /app/launches/<lid>/retry                   run a failed launch again (owner)
    GET/POST /app/campaigns/<cid>/launch/google      the same for Google (also behind GOOGLE_ADS)
    POST /app/campaigns/<cid>/launch/google/approve

All behind AD_LAUNCH (launches.on: off = 404, beta = AD_LAUNCH_WORKSPACES and admins) and the planner plan
(the plan page needs it). Members can draft and look; only the owner (or an admin) approves. Another
workspace's plan, card or launch is a 404. The approve button sends only the card id: what is approved is
the stored card the member was shown, re-checked on the server, never fields from the request.
"""
import json
import logging

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import adplatforms
import adpolicy
import adspec
import db
import launch_google
import launches
import plans
from core import feature_required, is_owner

log = logging.getLogger("campaignify.launch")
bp = Blueprint("launch", __name__)

APPROVALS_PER_HOUR = 10
UPDATES_PER_HOUR = 120


def _on(platform: str = "meta"):
    if not launches.on(g.get("ws"), g.get("user")) or not adplatforms.platform_on(platform, g.get("ws"), g.get("user")):
        abort(404)


def _plan(conn, cid):
    c = conn.execute("SELECT * FROM campaigns WHERE id=? AND workspace_id=?", (cid, g.ws["id"])).fetchone()
    if c is None or c["status"] != "ready":
        abort(404)
    return c


def _operator() -> bool:
    """An operator (admin) may try launching in a comped workspace; customers need a paid plan (D2)."""
    return bool(g.get("user")) and bool(g.user["is_admin"])


def _owner_ok() -> bool:
    return is_owner() and not g.get("viewing")


def _render(conn, c, card_row, conn_row):
    spec_d = json.loads(card_row["spec_json"])
    if spec_d.get("platform") == "google":
        return _render_google(conn, c, card_row, conn_row, spec_d)
    spec, issues, card, steps = launches.review(conn, g.ws, spec_d, conn_row, _operator())
    ads = launches.plan_ads(conn, g.ws["id"], c["id"])
    picked = {cr.get("asset_id"): cr for cr in spec_d.get("creatives") or []}
    rows = []
    for a in ads:
        cr = picked.get(a["asset_id"]) or {}
        rows.append({"a": a, "c": cr, "used": a["asset_id"] in picked})
    blocks = [i for i in issues if i.level == "block"]
    warns = [i for i in issues if i.level != "block"]
    pxs = launches.pixels_of(conn, g.ws["id"], conn_row["id"]) if conn_row is not None else []
    latest = launches.latest_for_plan(conn, g.ws["id"], c["id"], "meta")
    return render_template("launch_meta.html", c=c, card_row=card_row, spec=spec_d, card=card, blocks=blocks,
                           warns=warns, rows=rows, conn_row=conn_row, pixels=pxs, owner=_owner_ok(),
                           ceiling=launches.ceiling(g.ws, spec_d.get("currency"), _operator()),
                           ceiling_text=(launches.money(launches.ceiling(g.ws, spec_d.get("currency"), _operator()), spec_d.get("currency") or "USD")
                                         if launches.ceiling(g.ws, spec_d.get("currency"), _operator()) else ""),
                           minor_factor=adspec.minor_factor(spec_d.get("currency")),
                           plan_name=plans.PLANS[launches.launch_plan_code(g.ws, _operator())]["name"],
                           text=launches.card_text(card) if card else "", latest=latest, adspec=adspec,
                           cta_words=launches.CTA_WORDS, verticals=launches.VERTICAL_WORDS,
                           objectives=launches.OBJECTIVE_WORDS, steps=steps, placement_choices=adspec.META_CHOICES)


def _render_google(conn, c, card_row, conn_row, spec_d):
    spec, issues, card, steps = launches.review(conn, g.ws, spec_d, conn_row, _operator())
    images = launch_google.plan_images(conn, g.ws["id"], c["id"])
    picked = {cr.get("asset_id"): cr for cr in spec_d.get("creatives") or []}
    rows = [{"a": a, "used": a["asset_id"] in picked} for a in images]
    blocks = [i for i in issues if i.level == "block"]
    warns = [i for i in issues if i.level != "block"]
    cap = launches.ceiling(g.ws, spec_d.get("currency"), _operator())
    return render_template("launch_google.html", c=c, card_row=card_row, spec=spec_d, gs=spec_d.get("google") or {},
                           card=card, blocks=blocks, warns=warns, rows=rows, conn_row=conn_row, owner=_owner_ok(),
                           ceiling=cap, ceiling_text=launches.money(cap, spec_d.get("currency") or "USD") if cap else "",
                           minor_factor=adspec.minor_factor(spec_d.get("currency")),
                           plan_name=plans.PLANS[launches.launch_plan_code(g.ws, _operator())]["name"],
                           text=launches.card_text(card) if card else "",
                           latest=launches.latest_for_plan(conn, g.ws["id"], c["id"], "google"),
                           logos=launch_google.logo_choices(conn, g.ws["id"], c["product_id"]),
                           keyword_lines=launch_google.keyword_lines, objectives=launches.OBJECTIVE_WORDS,
                           verticals=launches.VERTICAL_WORDS, types=launch_google.TYPE_WORDS,
                           biddings=launch_google.BIDDING_WORDS, languages=launch_google.google.LANGUAGE_WORDS,
                           dashed=launch_google.google.dashed, steps=steps)


@bp.route("/app/campaigns/<int:cid>/launch/meta", methods=["GET", "POST"])
@feature_required("planner")
def page(cid):
    return _page(cid, "meta")


@bp.route("/app/campaigns/<int:cid>/launch/google", methods=["GET", "POST"])
@feature_required("planner")
def google_page(cid):
    return _page(cid, "google")


def _draft(conn, c, conn_row, platform):
    if platform == "google":
        return launch_google.draft(conn, g.ws, c, conn_row, request.args.get("type") or request.form.get("gtype"))
    return launches.draft(conn, g.ws, c, conn_row)


def _apply(conn, base, cid, platform):
    if platform == "google":
        return launch_google.apply_form(base, request.form, launch_google.plan_images(conn, g.ws["id"], cid))
    return launches.apply_form(base, request.form, launches.plan_ads(conn, g.ws["id"], cid))


def _page(cid, platform):
    _on(platform)
    endpoint = "launch.google_page" if platform == "google" else "launch.page"
    with db.connect() as conn:
        c = _plan(conn, cid)
        conn_row = launches.connection(conn, g.ws["id"], platform)
        card_row = None
        if request.method == "POST":
            if db.throttled(f"launch-card:{g.ws['id']}", UPDATES_PER_HOUR, 3600):
                flash("That's a lot of updates in an hour. Give it a few minutes.")
                return redirect(url_for(endpoint, cid=cid))
            db.hit(f"launch-card:{g.ws['id']}")
            base_row = None
            if (request.form.get("card") or "").isdigit():
                base_row = conn.execute("SELECT * FROM approval_cards WHERE id=? AND workspace_id=? AND campaign_id=?",
                                        (int(request.form["card"]), g.ws["id"], cid)).fetchone()
            base = json.loads(base_row["spec_json"]) if base_row else _draft(conn, c, conn_row, platform)
            if base.get("platform", "meta") != platform:
                abort(404)
            spec_d = _apply(conn, base, cid, platform)
            spec, issues, card, _ = launches.review(conn, g.ws, spec_d, conn_row, _operator())
            if spec is None:
                for i in issues[:3]:
                    flash(f"{i.message} {i.fix}".strip())
                return redirect(url_for(endpoint, cid=cid, card=base_row["id"] if base_row else None))
            card_id = launches.save_card(conn, g.ws["id"], g.user["id"], cid, conn_row, spec, card, issues)
            return redirect(url_for(endpoint, cid=cid, card=card_id) + "#card")
        if (request.args.get("card") or "").isdigit():
            card_row = conn.execute("SELECT * FROM approval_cards WHERE id=? AND workspace_id=? AND campaign_id=? AND "
                                    "kind='launch'", (int(request.args["card"]), g.ws["id"], cid)).fetchone()
            if card_row is None or json.loads(card_row["spec_json"] or "{}").get("platform", "meta") != platform:
                abort(404)
        if card_row is None:
            spec, issues, card, _ = launches.review(conn, g.ws, _draft(conn, c, conn_row, platform), conn_row, _operator())
            if spec is None:          # a plan whose draft doesn't fit the spec at all (shouldn't happen): show why
                for i in issues[:3]:
                    flash(i.message)
                return redirect(url_for("campaigns.show", cid=cid))
            card_id = launches.save_card(conn, g.ws["id"], g.user["id"], cid, conn_row, spec, card, issues)
            card_row = conn.execute("SELECT * FROM approval_cards WHERE id=?", (card_id,)).fetchone()
        return _render(conn, c, card_row, conn_row)


@bp.route("/app/campaigns/<int:cid>/launch/meta/approve", methods=["POST"])
@feature_required("planner")
def approve(cid):
    return _approve(cid, "meta")


@bp.route("/app/campaigns/<int:cid>/launch/google/approve", methods=["POST"])
@feature_required("planner")
def google_approve(cid):
    return _approve(cid, "google")


def _approve(cid, platform):
    _on(platform)
    endpoint = "launch.google_page" if platform == "google" else "launch.page"
    if not _owner_ok():
        abort(403, "Only the workspace owner can approve a launch.")
    with db.connect() as conn:
        _plan(conn, cid)
        raw = request.form.get("card") or ""
        card_row = conn.execute("SELECT * FROM approval_cards WHERE id=? AND workspace_id=? AND campaign_id=? AND "
                                "kind='launch'", (int(raw) if raw.isdigit() else 0, g.ws["id"], cid)).fetchone()
        if card_row is None or json.loads(card_row["spec_json"] or "{}").get("platform", "meta") != platform:
            abort(404)
        if (request.form.get("hash") or "") != card_row["spec_hash"]:
            flash("The card changed. Check it again, then approve.")
            return redirect(url_for(endpoint, cid=cid, card=card_row["id"]))
        if db.throttled(f"launch-approve:{g.ws['id']}", APPROVALS_PER_HOUR, 3600):
            flash("Too many launches in an hour. Try again later.")
            return redirect(url_for(endpoint, cid=cid, card=card_row["id"]))
        db.hit(f"launch-approve:{g.ws['id']}")
        try:
            lid, new = launches.approve(conn, g.ws, g.user, card_row)
        except launches.NotApproved as e:
            flash(str(e))
            return redirect(url_for(endpoint, cid=cid, card=card_row["id"]) + "#card")
    if new:
        flash(f"Approved. Campaignify is creating the paused campaign on {launches.PLATFORM_WORDS[platform]}.")
    return redirect(url_for("launch.status", lid=lid))


def _launch(conn, lid):
    L = conn.execute("SELECT * FROM launches WHERE id=? AND workspace_id=?", (lid, g.ws["id"])).fetchone()
    if L is None or (L["platform"] != "meta" and not adplatforms.platform_on(L["platform"], g.get("ws"), g.get("user"))):
        abort(404)
    return L


@bp.route("/app/launches/<int:lid>")
@feature_required("planner")
def status(lid):
    _on()
    with db.connect() as conn:
        L = _launch(conn, lid)
        v = launches.view(conn, L)
        c = conn.execute("SELECT * FROM campaigns WHERE id=? AND workspace_id=?", (L["plan_id"] or 0, g.ws["id"])).fetchone()
    return render_template("launch_status.html", v=v, L=L, c=c, owner=_owner_ok())


@bp.route("/app/launches/<int:lid>.json")
@feature_required("planner")
def status_json(lid):
    _on()
    with db.connect() as conn:
        L = _launch(conn, lid)
        v = launches.view(conn, L)
    return jsonify({"status": L["status"], "busy": v["busy"], "error": L["error"],
                    "steps": [{"label": s["label"], "status": s["status"], "word": s["word"]} for s in v["steps"]]})


@bp.route("/app/launches/<int:lid>/retry", methods=["POST"])
@feature_required("planner")
def retry(lid):
    _on()
    if not _owner_ok():
        abort(403, "Only the workspace owner can run a launch again.")
    with db.connect() as conn:
        L = _launch(conn, lid)
        if L["status"] not in ("failed", "rolled_back"):
            return redirect(url_for("launch.status", lid=lid))
        card_row = conn.execute("SELECT * FROM approval_cards WHERE id=? AND workspace_id=?",
                                (L["card_id"] or 0, g.ws["id"])).fetchone()
        if card_row is None:
            abort(404)
        # The same checks as the first approval: plan, ceiling, connection, library, policy.
        conn_row = conn.execute("SELECT * FROM ad_connections WHERE id=? AND workspace_id=?",
                                (L["connection_id"], g.ws["id"])).fetchone()
        spec, issues, _, _ = launches.review(conn, g.ws, json.loads(L["spec_json"]), conn_row, _operator())
        if spec is None or adpolicy.blocking(issues):
            first = next((i for i in issues if i.level == "block"), None)
            flash(first.message if first else "The launch has problems to fix first.")
            return redirect(url_for("launch.status", lid=lid))
        launches.requeue(conn, lid)
    flash(f"Running it again. What {launches.PLATFORM_WORDS.get(L['platform'], 'Meta')} still has from the last try is "
          "reused, not made twice.")
    return redirect(url_for("launch.status", lid=lid))


@bp.app_context_processor
def _slot():
    """launch_slot(c, platform): what the plan page's Meta (or Google) block shows in the launch kit (the "Create
    paused campaign" button). Google's needs GOOGLE_ADS as well."""
    def launch_slot(c, platform="meta"):
        if c is None or not launches.on(g.get("ws"), g.get("user")) or c["status"] != "ready" \
                or not adplatforms.platform_on(platform, g.get("ws"), g.get("user")):
            return None
        with db.connect() as conn:
            latest = launches.latest_for_plan(conn, g.ws["id"], c["id"], platform)
            connected = launches.connection(conn, g.ws["id"], platform) is not None
        return {"url": url_for("launch.google_page" if platform == "google" else "launch.page", cid=c["id"]),
                "connected": connected, "connect": url_for("adconnect.settings"), "latest": latest,
                "platform": platform, "word": launches.PLATFORM_WORDS[platform],
                "latest_url": url_for("launch.status", lid=latest["id"]) if latest else None}
    return {"launch_slot": launch_slot}

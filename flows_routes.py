"""Flows UI: the list, the editor (steps / timeline / activity), the AI
drafter, turning on and pausing, and the tasks inbox."""
import json
import secrets

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import copywriter
import db
import flows
import flows_engine
import llm
import plans
from core import brand, can_afford, charge_ai, feature_required, login_required

bp = Blueprint("flows", __name__)
TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
             "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "America/Vancouver", "Europe/London",
             "Europe/Dublin", "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Australia/Sydney", "Asia/Singapore",
             "UTC"]


def _flow(conn, fid):
    f = conn.execute("SELECT * FROM flows WHERE id=? AND workspace_id=? AND status<>'archived'", (fid, g.ws["id"])).fetchone()
    if not f:
        abort(404)
    return f


def _forms(conn):
    return [dict(r) for r in conn.execute("SELECT id, name FROM forms WHERE workspace_id=? ORDER BY id DESC", (g.ws["id"],))]


def channels() -> dict:
    """What can actually send right now (email from F1, SMS from F3; both on the workspace's plan)."""
    return {"email": "email" in flows_engine.SENDERS and _feature("flows_email"),
            "sms": "sms" in flows_engine.SENDERS and _feature("flows_sms")}


def _feature(key):
    return key in plans.FEATURES and plans.has(g.ws, key)


def readiness(conn, f, tree, probs) -> list[dict]:
    """The Turn-on checklist: blocking items (ok False, block True) and plain notes."""
    out = []
    t = tree["trigger"]
    out.append({"ok": "trigger" not in probs, "block": True, "label": "What starts it",
                "detail": " ".join(probs.get("trigger", [])) or flows.TRIGGERS.get(t.get("kind"), "")})
    step_probs = {k: v for k, v in probs.items() if k not in ("trigger",)}
    out.append({"ok": not step_probs, "block": True, "label": "Every step is complete and follows the rules",
                "detail": f"{sum(len(v) for v in step_probs.values())} thing(s) to fix" if step_probs else ""})
    ch = channels()
    kinds = {s["type"] for s, _ in flows.walk(tree["steps"])}
    if "email" in kinds:
        out.append({"ok": ch["email"], "block": False, "label": "Email sending",
                    "detail": "Connected." if ch["email"] else "Not connected yet: each email becomes a task for you, "
                    "with the copy ready to paste."})
    if "sms" in kinds:
        out.append({"ok": ch["sms"], "block": False, "label": "Text messages",
                    "detail": "Connected." if ch["sms"] else "Not connected yet: each text becomes a task for you."})
    if t.get("kind") in ("form_submitted", "contact_added", "tag_added", "date") and not plans.has(g.ws, "forms"):
        out.append({"ok": False, "block": False, "label": "Contacts",
                    "detail": f"Forms and contacts come with {plans.required('forms')['name']}; until then nobody can "
                    "enter this flow."})
    return out


def _stats(conn, fid) -> dict:
    row = conn.execute("SELECT SUM(status='waiting') active, SUM(status='done') done, SUM(status IN ('exited')) exited, "
                       "SUM(status='failed') failed, COUNT(*) total FROM flow_runs WHERE flow_id=?", (fid,)).fetchone()
    tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE flow_id=? AND done_at IS NULL", (fid,)).fetchone()[0]
    return {k: row[k] or 0 for k in ("active", "done", "exited", "failed", "total")} | {"tasks": tasks}


@bp.route("/app/flows")
@feature_required("flows")
def index():
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM flows WHERE workspace_id=? AND status<>'archived' ORDER BY updated_at DESC",
                            (g.ws["id"],)).fetchall()
        items = []
        for f in rows:
            tree = flows.unpack(f["graph_json"])["tree"]
            items.append({"f": f, "trigger": flows.TRIGGERS.get(tree["trigger"].get("kind"), "No trigger yet"),
                          "steps": sum(1 for _ in flows.walk(tree["steps"])), "stats": _stats(conn, f["id"])})
        open_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE workspace_id=? AND done_at IS NULL",
                                  (g.ws["id"],)).fetchone()[0]
        forms = _forms(conn)
    return render_template("flows.html", items=items, templates=flows.TEMPLATES, ai=llm.anthropic_ready(),
                           quote=llm.estimate_credits(*flows.DRAFT_TOKENS), open_tasks=open_tasks, forms=forms,
                           timezones=TIMEZONES, tz=g.ws["timezone"] or "America/Chicago", channels=channels())


def _create(conn, name, tree) -> int:
    t = db.now()
    fid = conn.execute("INSERT INTO flows (workspace_id, name, status, graph_json, settings_json, created_by, created_at, "
                       "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       (g.ws["id"], name[:80] or "Untitled flow", "draft", flows.pack(tree),
                        json.dumps({"webhook_secret": secrets.token_hex(16)}), g.user["id"], t, t)).lastrowid
    conn.commit()
    return fid


@bp.route("/app/flows/new", methods=["POST"])
@feature_required("flows")
def new():
    kind = request.form.get("kind")
    with db.connect() as conn:
        forms = _forms(conn)
        if kind == "template" and request.form.get("template") in flows.TEMPLATE_BY_ID:
            t = flows.TEMPLATE_BY_ID[request.form["template"]]
            tree = flows.from_template(t["id"])
            if tree["trigger"].get("kind") == "form_submitted" and forms:
                tree["trigger"]["form_id"] = forms[0]["id"]
            fid = _create(conn, t["name"], tree)
        else:
            fid = _create(conn, (request.form.get("name") or "").strip() or "Untitled flow",
                          {"trigger": {"kind": "form_submitted", "form_id": forms[0]["id"] if forms else None}, "steps": []})
    return redirect(url_for("flows.edit", fid=fid))


@bp.route("/app/flows/draft", methods=["POST"])
@feature_required("flows")
def draft():
    """Describe the funnel in plain words -> a draft flow (off) to edit. Charged at cost."""
    text = (request.form.get("text") or "").strip()[:1500]
    if len(text) < 12:
        flash("Describe the flow in a sentence or two.")
        return redirect(url_for("flows.index"))
    if not llm.anthropic_ready():
        flash("AI drafting isn't switched on yet. Start from a template instead.")
        return redirect(url_for("flows.index"))
    quote = llm.estimate_credits(*flows.DRAFT_TOKENS)
    if not can_afford(quote):
        flash(f"Drafting needs about {quote} credits and you have {db.balance(g.ws):,}.")
        return redirect(url_for("flows.index"))
    with db.connect() as conn:
        forms = _forms(conn)
        b = brand()
        examples = "\n".join(f"- {t['name']}: " + "; ".join(f"day {r['day']}: {r['label']}" for r in
                                                            flows.timeline(flows.from_template(t['id']))[:6])
                             for t in flows.TEMPLATES[:3])
        prompt = (copywriter.brief_text(b, {}) + "\n\nForms they have: " + (", ".join(f["name"] for f in forms) or "none") +
                  f"\nEmail sending: {'connected' if channels()['email'] else 'not connected yet (steps become tasks)'}."
                  f"\nTexts allowed only if the request below asks for them.\n\nExamples of good timing:\n{examples}\n\n"
                  f"What they want:\n{text}")
        usage = llm.Usage()
        try:
            out = llm.structured(system=flows.draft_system(copywriter.RULES), prompt=prompt, schema=flows.DRAFT_SCHEMA,
                                 name="submit_flow", max_tokens=4000, usage=usage, temperature=0.5)
            tree = flows.from_draft(out)
            if not any(w in text.lower() for w in ("text", "sms")):   # texts only when they were asked for
                tree = flows.without(tree, "sms")
            if tree["trigger"].get("kind") == "form_submitted" and forms and not tree["trigger"].get("form_id"):
                tree["trigger"]["form_id"] = forms[0]["id"]
            err = None if tree["steps"] else "The draft came back empty. Try describing it differently."
        except llm.LLMError as e:
            tree, err, out = None, f"The writer didn't finish ({e}). Nothing was saved.", {}
        taken = charge_ai(conn, usage.credits, "AI flow draft") if usage.calls else 0
        conn.commit()
        if err:
            flash(err)
            return redirect(url_for("flows.index"))
        fid = _create(conn, str(out.get("name") or "Drafted flow")[:80], tree)
    flash(f"Drafted for {taken} credit{'s' if taken != 1 else ''}. It's off: check every step, then turn it on.", "ok")
    return redirect(url_for("flows.edit", fid=fid))


def _boot(conn, f):
    d = flows.unpack(f["graph_json"])
    tree = d["tree"]
    forms = _forms(conn)
    probs = flows.problems(tree, forms)
    tags = set()
    for r in conn.execute("SELECT tags FROM contacts WHERE workspace_id=?", (g.ws["id"],)):
        tags |= {t for t in (r["tags"] or "").split(",") if t}
    for s, _ in flows.walk(tree["steps"]):
        if s.get("tag"):
            tags.add(s["tag"])
    pages = [dict(r) for r in conn.execute("SELECT slug, title FROM pages WHERE workspace_id=? ORDER BY id DESC",
                                           (g.ws["id"],))]
    return {"flow": {"id": f["id"], "name": f["name"], "status": f["status"], "version": f["version"],
                     "dirty": bool(f["dirty"])},
            "tree": tree, "forms": forms, "tags": sorted(tags), "pages": pages, "types": flows.STEP_TYPES,
            "triggers": flows.TRIGGERS, "tests": flows.TESTS, "stages": flows.STAGES, "problems": probs,
            "timeline": flows.timeline(tree, {x["id"]: x["name"] for x in forms}), "channels": channels(),
            "brand": brand(), "readiness": readiness(conn, f, tree, probs),
            "urls": {"save": url_for("flows.save", fid=f["id"]), "state": url_for("flows.state", fid=f["id"])}}


@bp.route("/app/flows/<int:fid>")
@feature_required("flows")
def edit(fid):
    with db.connect() as conn:
        f = _flow(conn, fid)
        boot = _boot(conn, f)
        stats = _stats(conn, fid)
        tree = boot["tree"]
        names = {s["id"]: flows.describe(s) for s, _ in flows.walk(tree["steps"])}
        by_node = {r["node_id"]: dict(r) for r in conn.execute(
            "SELECT node_id, SUM(kind='task') tasks, SUM(kind='sent') sent, SUM(kind='branch' AND detail='yes') yes, "
            "SUM(kind='branch' AND detail='no') no, SUM(kind='error') errors, COUNT(*) n FROM flow_events WHERE flow_id=? "
            "AND node_id IS NOT NULL GROUP BY node_id", (fid,))}
        at_node = {r["node_id"]: r["n"] for r in conn.execute(
            "SELECT node_id, COUNT(*) n FROM flow_runs WHERE flow_id=? AND status='waiting' GROUP BY node_id", (fid,))}
        events = conn.execute("SELECT e.*, c.email, c.name cname FROM flow_events e LEFT JOIN contacts c ON c.id=e.contact_id "
                              "WHERE e.flow_id=? ORDER BY e.id DESC LIMIT 60", (fid,)).fetchall()
        runs = conn.execute("SELECT r.*, c.email, c.name cname FROM flow_runs r JOIN contacts c ON c.id=r.contact_id "
                            "WHERE r.flow_id=? AND r.status='waiting' ORDER BY r.wake_at LIMIT 100", (fid,)).fetchall()
        day30 = conn.execute("SELECT substr(started_at,1,10) d, COUNT(*) n FROM flow_runs WHERE flow_id=? AND "
                             "started_at>=date('now','-30 day') GROUP BY d ORDER BY d", (fid,)).fetchall()
    return render_template("flow_edit.html", f=f, boot=boot, stats=stats, names=names, by_node=by_node, at_node=at_node,
                           events=events, runs=runs, day30=day30, tab=request.args.get("tab") or "steps")


@bp.route("/app/flows/<int:fid>/save", methods=["POST"])
@feature_required("flows")
def save(fid):
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        abort(400)
    try:
        tree = flows.clean_tree(d.get("tree") or {})
    except flows.FlowError as e:
        return jsonify({"error": str(e)}), 400
    with db.connect() as conn:
        f = _flow(conn, fid)
        name = " ".join(str(d.get("name") or f["name"]).split())[:80] or f["name"]
        conn.execute("UPDATE flows SET name=?, graph_json=?, dirty=?, updated_at=? WHERE id=?",
                     (name, flows.pack(tree), 1 if f["version_id"] else 0, db.now(), fid))
        conn.commit()
        f = _flow(conn, fid)
        boot = _boot(conn, f)
    return jsonify({"ok": True, "tree": boot["tree"], "problems": boot["problems"], "timeline": boot["timeline"],
                    "readiness": boot["readiness"], "flow": boot["flow"]})


@bp.route("/app/flows/<int:fid>/state", methods=["POST"])
@feature_required("flows")
def state(fid):
    action = request.form.get("action")
    with db.connect() as conn:
        f = _flow(conn, fid)
        tree = flows.unpack(f["graph_json"])["tree"]
        probs = flows.problems(tree, _forms(conn))
        if action in ("on", "publish"):
            if probs:
                flash("Fix the highlighted steps first: " + "; ".join(sum(probs.values(), []))[:300])
                return redirect(url_for("flows.edit", fid=fid))
            if action == "publish" or not f["version_id"] or f["dirty"]:
                flows_engine.publish(conn, f)
            if action == "on":
                conn.execute("UPDATE flows SET status='on', turned_on_at=COALESCE(turned_on_at, ?), errors=0 WHERE id=?",
                             (db.now(), fid))
                flash("The flow is on. New people who match the trigger start at the first step.", "ok")
            else:
                flash("Changes published. People already in the flow finish the version they started; new people get "
                      "this one.", "ok")
        elif action == "pause":
            conn.execute("UPDATE flows SET status='paused' WHERE id=?", (fid,))
            flash("Paused. Nobody new enters, and people in it wait where they are until you turn it on again.", "ok")
        elif action == "duplicate":
            nid = _create(conn, f["name"] + " (copy)", flows.clean_tree(json.loads(json.dumps(tree))))
            return redirect(url_for("flows.edit", fid=nid))
        elif action == "archive":
            conn.execute("UPDATE flows SET status='archived' WHERE id=?", (fid,))
            conn.execute("UPDATE flow_runs SET status='exited', exit_reason='flow deleted', ended_at=?, lease_until=NULL "
                         "WHERE flow_id=? AND status='waiting'", (db.now(), fid))
            conn.commit()
            flash("Flow deleted. Its history stays in each contact's timeline.", "ok")
            return redirect(url_for("flows.index"))
        conn.execute("UPDATE flows SET updated_at=? WHERE id=?", (db.now(), fid))
        conn.commit()
    return redirect(url_for("flows.edit", fid=fid))


@bp.route("/app/flows/runs/<int:rid>/exit", methods=["POST"])
@feature_required("flows")
def exit_run(rid):
    with db.connect() as conn:
        ok = flows_engine.exit_run(conn, rid, g.ws["id"], "Taken out by " + (g.user["email"] or "a member"))
        conn.commit()
    flash("Taken out of the flow." if ok else "That run had already finished.", "ok" if ok else "")
    nxt = request.form.get("back") or url_for("flows.index")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("flows.index"))


@bp.route("/app/flows/settings", methods=["POST"])
@feature_required("flows")
def settings():
    tz = request.form.get("timezone")
    if tz in TIMEZONES:
        with db.connect() as conn:
            conn.execute("UPDATE workspaces SET timezone=? WHERE id=?", (tz, g.ws["id"]))
            conn.commit()
        flash("Time zone saved.", "ok")
    return redirect(url_for("flows.index"))


@bp.route("/app/tasks")
@login_required
def tasks():
    show = request.args.get("show") or "open"
    with db.connect() as conn:
        rows = conn.execute("SELECT t.*, c.email, c.name cname, f.name fname FROM tasks t LEFT JOIN contacts c ON "
                            "c.id=t.contact_id LEFT JOIN flows f ON f.id=t.flow_id WHERE t.workspace_id=? AND "
                            + ("t.done_at IS NULL" if show == "open" else "t.done_at IS NOT NULL") +
                            " ORDER BY " + ("t.due_at, t.id" if show == "open" else "t.done_at DESC") + " LIMIT 300",
                            (g.ws["id"],)).fetchall()
        n_open = conn.execute("SELECT COUNT(*) FROM tasks WHERE workspace_id=? AND done_at IS NULL", (g.ws["id"],)).fetchone()[0]
    return render_template("tasks.html", rows=rows, show=show, n_open=n_open, now=db.now())


@bp.route("/app/tasks/<int:tid>/done", methods=["POST"])
@login_required
def task_done(tid):
    undo = request.form.get("undo") == "1"
    with db.connect() as conn:
        conn.execute("UPDATE tasks SET done_at=?, done_by=? WHERE id=? AND workspace_id=?",
                     (None if undo else db.now(), None if undo else g.user["id"], tid, g.ws["id"]))
        conn.commit()
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True})
    return redirect(url_for("flows.tasks", show=request.form.get("show") or "open"))

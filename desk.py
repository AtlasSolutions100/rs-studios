"""The campaign desk (the new look only): one sentence in, a whole campaign back, priced before anything is made.

With the new look on, this is the app's home once a member has made something (their first run keeps Studio's own
opening move, the two free image ads). The sentence goes through Studio's Ask (studio_v2.ask_run: the same model call,
the same daily cap, the same claim rules), which answers with the ads. The rest of the campaign is added here by
word rules and the member's plan, with no model call:

    Make        the ads Ask proposed (image ads, a presenter video, a voiceover, a product video)
    Write       ad copy for one platform (copywriter.write, held at its estimate and settled at what it used)
    Capture     a page with a sign-up form, saved as a draft (pages.starter_blocks, the form's default fields)
    Follow up   an email follow-up from a template, saved switched off (flows.from_template)
    Launch      the hand-over to the ad accounts, as the step after the ads are done

Every part is a row the member can untick, edit in place or remove, and each row carries its price. The total is on
the button that runs it, and /app/desk/run starts nothing unless the total it is sent is the total it works out
(409 otherwise), exactly as Studio does. What one confirmation made is kept in desk_runs, so the desk can show the
campaign again with a link to each thing.

A part is a small dict the browser holds and sends back; clean_part() cuts it down to the fields its kind may set, so
what is priced and started is never more than that.
"""
import json
import logging
import re

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, url_for

import catalog
import compliance
import copywriter
import create_routes
import credits as credit_holds
import db
import flows
import llm
import pages
import pipeline
import plans
import platforms
import products
import studio_v2
import ui_v2
from core import brand, login_required, reserve_ai

log = logging.getLogger("desk")
bp = Blueprint("desk", __name__)

MAKES = studio_v2.ASK_MAKES
STAGES = [("make", "Make"), ("write", "Write"), ("capture", "Capture"), ("followup", "Follow up"), ("launch", "Launch")]
STAGE_OF = {"image": "make", "presenter": "make", "voiceover": "make", "product_video": "make",
            "copy": "write", "page": "capture", "flow": "followup", "launch": "launch"}
STAGE_ORDER = {key: i for i, (key, _name) in enumerate(STAGES)}
SPINE = ("copy", "page", "flow", "launch")                 # the rest of a campaign, in the order it happens
FEATURE = {"copy": "copy", "page": "pages", "flow": "flows", "launch": "adhub"}
ENDPOINT = {"copy": "copy.studio", "page": "pages.index", "flow": "flows.index", "launch": "adhub.index"}
ADD_NAMES = {"image": "Image ads", "presenter": "Presenter video", "voiceover": "Voiceover video",
             "product_video": "Product video", "copy": "Ad copy", "page": "A page with a sign-up form",
             "flow": "An email follow-up", "launch": "Launch"}
DEFAULT_FLOW = "lead_magnet"
MAX_PARTS = 12
RUNS_SHOWN = 4

# What the sentence asks for beyond ads: Studio's own word rules (studio_v2.ASK_ALSO), by the tool they point at, plus
# the few ways people say "page" and "follow-up" when they are describing a campaign and not naming a tool.
_ALSO_RX = {ep: rx for ep, _name, _line, rx, _feat in studio_v2.ASK_ALSO}
WANTS = {
    "copy": r"\b(copy|captions?|primary text|ad text|hooks?)\b",
    "page": _ALSO_RX["pages.index"] + r"|\b(sign[- ]?ups?|wait(?:ing)? ?list|lead forms?|a form|collect (?:names|emails|leads))\b",
    "flow": _ALSO_RX["flows.index"] + r"|\b(email (?:sequence|series)|emails? (?:after|over|to (?:new|everyone))|"
                                      r"(?:two|three|four|five|six|\d)[- ]emails?)\b",
    "launch": _ALSO_RX["adhub.index"],
}


def wanted(text: str) -> set:
    t = " ".join(str(text or "").lower().split())
    return {kind for kind, rx in WANTS.items() if re.search(rx, t)}


def is_home() -> bool:
    """Is the desk this member's home? Yes once they have made something, unless the link asks Studio for
    something (a mode, a batch to edit, a preset)."""
    if any(request.args.get(k) for k in ("mode", "batch", "preset")):
        return False
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (g.ws["id"],)).fetchone() is not None


def _has(kind: str) -> bool:
    """Is this kind of part something the app can do here at all (the tool is installed)?"""
    return kind in MAKES or ENDPOINT[kind] in current_app.view_functions


def _locked(kind: str) -> str:
    """The plan that includes this part, when the member's plan doesn't ("" when it does)."""
    feat = FEATURE.get(kind)
    return plans.required(feat).name if feat and not plans.has(g.ws, feat) else ""


def flow_templates() -> list[dict]:
    """The follow-ups the desk offers: the email-only templates (a text needs its own setup and consent)."""
    return [{"id": t["id"], "name": t["name"], "line": t["line"]} for t in flows.TEMPLATES if t["channels"] == ["email"]]


def clean_part(raw, cast, have) -> dict | None:
    """One row, cut down to what its kind may set (None when it is not a part)."""
    if not isinstance(raw, dict) or raw.get("kind") not in STAGE_OF or not _has(raw["kind"]):
        return None
    kind = raw["kind"]
    part = {"id": re.sub(r"[^A-Za-z0-9]", "", str(raw.get("id") or ""))[:12], "kind": kind, "on": bool(raw.get("on"))}
    if kind in MAKES:
        if kind not in have:
            return None
        it = studio_v2.ask_item(dict(raw, make=kind), cast, have)
        if it is None:
            if kind == "presenter" and not cast:
                return None
            part.update(text="", on=False)              # no words yet: the row waits, unticked, until it has some
            if kind == "image":
                part["count"] = 1
            if kind == "presenter":
                part["presenter"] = cast[0]["id"]
        else:
            part.update({k: it[k] for k in ("text", "count", "presenter", "situation") if k in it})
    elif kind == "copy":
        part["platform"] = raw.get("platform") if raw.get("platform") in platforms.PLATFORMS else "meta"
        part["n"] = 5 if str(raw.get("n")) == "5" else 3
    elif kind == "page":
        part["title"] = studio_v2._clean(raw.get("title"), 120)
    elif kind == "flow":
        ok = {t["id"] for t in flow_templates()}
        part["template"] = raw.get("template") if raw.get("template") in ok else DEFAULT_FLOW
    if _locked(kind):
        part["on"] = False
    return part


def clean_parts(raw, cast=None, have=None) -> list[dict]:
    cast = cast if cast is not None else studio_v2.roster()
    have = have if have is not None else {m["id"] for m in studio_v2.available_modes()}
    out, makes, seen = [], 0, set()
    for x in (raw if isinstance(raw, list) else [])[:MAX_PARTS * 2]:
        p = clean_part(x, cast, have)
        if p is None or len(out) >= MAX_PARTS:
            continue
        if p["kind"] in MAKES:
            makes += 1
            if makes > studio_v2.ASK_MAX_PARTS:     # Studio starts at most this many parts from one confirmation
                continue
        elif p["kind"] in seen:                     # one of each of the rest
            continue
        seen.add(p["kind"])
        if not p["id"] or p["id"] in {o["id"] for o in out}:
            p["id"] = f"p{len(out) + 1}x"
        out.append(p)
    out.sort(key=lambda p: STAGE_ORDER[STAGE_OF[p["kind"]]])
    return out


def _make_item(part) -> dict:
    return {"make": part["kind"], "text": part["text"], "count": part.get("count"), "presenter": part.get("presenter"),
            "situation": part.get("situation")}


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def view(part, product, cast) -> dict:
    """The row as the member reads it: its stage, a name, the words, the price and what Edit can change."""
    kind = part["kind"]
    v = dict(part, stage=STAGE_OF[kind], locked=_locked(kind), price=None, label="", error="", words="")
    if kind in MAKES:
        v["words"] = part["text"]
        if not part["text"]:
            v.update(name=ADD_NAMES[kind], label="Add the words", on=False)
            return v
        it = studio_v2.ask_item(_make_item(part), cast)
        v["name"] = it["name"] if it else ADD_NAMES[kind]
        q = studio_v2.quote_parts([studio_v2._ask_part(it, product["id"])]) if it else {"error": "That can't be made right now."}
        if q.get("error"):
            v.update(error=q["error"], on=False, label="Can't be priced")
        else:
            v.update(price=q["total"], label=f"{q['total']:,} credits", videos=q["count"] if q.get("videos") else 0)
    elif kind == "copy":
        v["name"] = f"Ad copy for {platforms.PLATFORMS[part['platform']]['short']}"
        v["words"] = f"{part['n']} versions of every text field, in your voice, from your approved claims."
        v["price"] = copywriter.estimate(part["n"], "ad")
        v["label"] = f"Up to {_plural(v['price'], 'credit')}"
    elif kind == "page":
        title = part["title"] or product["name"]
        forms_on = plans.has(g.ws, "forms") and "forms.index" in current_app.view_functions
        v["name"] = "A page with a sign-up form" if forms_on else "A page for the offer"
        v["words"] = (f"“{title}”" + (", asking for a name and an email" if forms_on else "") +
                      ". Saved as a draft: you publish it.")
        v["label"] = "In your plan"
        v["title"] = title
    elif kind == "flow":
        t = flows.TEMPLATE_BY_ID[part["template"]]
        v["name"] = f"Email follow-up: {t['name']}"
        v["words"] = f"{t['line']} Saved switched off: you turn it on."
        v["label"] = "In your plan"
    elif kind == "launch":
        v["name"] = "Get the ads into your ad accounts"
        v["words"] = "The step after the ads are done. You pick the account, and nothing runs until you say so."
        v["label"] = "No credits"
    if v["locked"]:
        v.update(on=False, price=None, label=f"{v['locked']} plan")
    return v


def price(parts, product, cast=None) -> dict:
    """Every row with its price, and the total of the ticked ones. No model call: the ads are priced by Create's own
    quote (through Studio), the copy at the most it can cost."""
    cast = cast if cast is not None else studio_v2.roster()
    views = [view(p, product, cast) for p in parts]
    on = [v for v in views if v["on"]]
    ads = sum(v["price"] or 0 for v in on if v["kind"] in MAKES)
    copy_max = sum(v["price"] or 0 for v in on if v["kind"] == "copy")
    videos = sum(v.get("videos") or 0 for v in on)
    total, bal = ads + copy_max, db.balance(g.ws)
    return {"parts": views, "total": total, "ads": ads, "copy": copy_max, "count": len(on), "balance": bal,
            "short": max(0, total - bal), "pack": create_routes.suggest_pack(total - bal) if total > bal else None,
            "confirm": videos > create_routes.CONFIRM_VIDEOS, "videos": videos, "product": product["id"]}


def _product(conn, pid):
    p = products.get_product(conn, g.ws["id"], pid) if pid else None
    if p is None or p["brand_id"] not in products.unlocked_brand_ids(conn, g.ws):
        return None
    return p


def starters(conn, product) -> dict:
    """First words for a part the member adds by hand, from what the product already says (no model)."""
    scenes = studio_v2.scene_templates(conn, product["id"], n=1)
    script = studio_v2.starter_script(product)
    return {"image": (scenes[0]["headline"] if scenes else "") or product["name"],
            "presenter": script, "voiceover": script,
            "product_video": f"{product['name']} on a table in soft light, the camera moving in slowly."}


# ---------------------------------------------------------------- the routes

@bp.route("/app/desk/plan", methods=["POST"])
@login_required
def plan():
    """The sentence -> the campaign, priced. Nothing is made here."""
    if not ui_v2.on():
        return studio_v2._off()
    d = studio_v2._payload()
    text = studio_v2._clean(d.get("ask"), 600)
    out, status = studio_v2.ask_run(text, d.get("product"))
    if status == 400 and "step" not in out:             # too short, a claim the ads can't make, no product
        return jsonify(out), 400
    with db.connect() as conn:
        product = _product(conn, d.get("product"))
    if product is None:
        return jsonify({"error": "Add your product first, then ask again."}), 400
    cast = studio_v2.roster()
    have = {m["id"] for m in studio_v2.available_modes()}
    want, raw = wanted(text), []
    if status == 200:
        note = out.get("note") or "Here is a campaign for that."
        raw = [dict(it, kind=it["make"], on=True) for it in out["plan"]]
    elif status == 429:
        note = (f"That's {studio_v2.ASK_DAILY} asks today, so I haven't planned the ads. Add them with Add a part, "
                "or ask again tomorrow.")
    elif status == 422:
        note = "No ads in this one. Add some with Add a part if you want them."
    elif status == 400:
        note = out.get("error") or ""
    else:
        note = "The ads didn't come back this time. Add them with Add a part, or plan it again."
    raw += [{"kind": k, "on": k in want} for k in SPINE]
    for i, r in enumerate(raw):
        r["id"] = f"p{i + 1}"
    ans = price(clean_parts(raw, cast, have), product, cast)
    rows = {url_for(ENDPOINT[k]) for k in SPINE if _has(k)}          # already a row of the plan: not a link as well
    also = [a for a in (out.get("also") or studio_v2.ask_also(text)) if a["href"] not in rows]
    ans.update(note=note, skipped=out.get("skipped") or [], also=also, ask=text, planned=status == 200,
               left=out.get("left"))
    return jsonify(ans)


@bp.route("/app/desk/quote", methods=["POST"])
@login_required
def quote():
    """The rows as they stand now, re-priced (a tick, an edit, a part added or removed)."""
    if not ui_v2.on():
        return studio_v2._off()
    d = studio_v2._payload()
    with db.connect() as conn:
        product = _product(conn, d.get("product"))
    if product is None:
        return jsonify({"error": "Pick a product first."}), 400
    return jsonify(price(clean_parts(d.get("parts")), product))


def _write_copy(part, product, said) -> tuple[dict | None, str]:
    """Write the ad copy this row stands for: (what was made, "") or (None, why not). Held at the estimate before the
    writer runs and settled at what it used, as the Copy page does."""
    import copy_routes
    if not llm.configured():
        return None, "The copy wasn't written (AI writing isn't switched on), so it wasn't charged."
    b = brand(product["id"])
    brief = {"offer": (b.get("offer") or "")[:300], "audience": (b.get("audience") or "")[:200], "goal": "", "tone": "",
             "link": (b.get("url") or "")[:300], "notes": said[:1000]}
    if not compliance.check(brief["offer"], brief["audience"], brief["notes"])["ok"]:
        return None, "The copy wasn't written: the request asks for a claim it can't make. It wasn't charged."
    n, kind, platform = part["n"], "ad", part["platform"]
    hold = reserve_ai(copywriter.estimate(n, kind), "AI ad copy")
    if hold is None:
        return None, "The copy wasn't written: not enough credits left for it. It wasn't charged."
    usage = llm.Usage()
    try:
        variants = copywriter.write(b, kind, platform, brief, n, usage)
    except llm.LLMError as e:
        credit_holds.settle(hold, usage.credits if usage.calls else 0)
        return None, f"The copy didn't finish ({e}). Write it again from Copy."
    except Exception:
        credit_holds.release(hold, "the writer crashed")
        raise
    with db.connect() as conn:
        rid = copy_routes.save_run(conn, kind, platform, dict(brief, n=n), {"variants": variants}, usage,
                                   product_id=product["id"], hold=hold)
    return {"kind": "copy", "name": f"Ad copy for {platforms.PLATFORMS[platform]['short']}", "state": "Written",
            "href": url_for("copy.run", rid=rid)}, ""


def _make_page(conn, part, product) -> tuple[list, str, int | None]:
    """A draft page for the product, with a sign-up form when the plan has forms: (what was made, why not, form id)."""
    import forms_routes
    import pages_routes
    import secrets
    if conn.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0] >= pages_routes.MAX_DRAFTS:
        return [], f"The page wasn't made: you have {pages_routes.MAX_DRAFTS} pages. Delete one to make another.", None
    title = part["title"] or product["name"]
    made, fid, t = [], None, db.now()
    blocks = pages.starter_blocks(brand(product["id"]))
    if plans.has(g.ws, "forms") and "forms.index" in current_app.view_functions:
        fid = conn.execute("INSERT INTO forms (workspace_id, code, name, fields_json, button_text, success_message, "
                           "webhook_secret, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (g.ws["id"], secrets.token_urlsafe(9), f"{title} sign-up"[:80],
                            json.dumps(forms_routes.DEFAULT_FIELDS), "Sign up", "Thanks! We'll be in touch.",
                            secrets.token_hex(16), t, t)).lastrowid
        block = pages.clean_block("form", {"heading": "Sign up", "form": fid}, lambda _id: False, lambda _id: True)
        if block:
            blocks.insert(1, block)
        made.append({"kind": "form", "name": "Sign-up form", "state": "Ready", "href": url_for("forms.edit", fid=fid)})
    pid = conn.execute("INSERT INTO pages (workspace_id, slug, title, description, blocks_json, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (g.ws["id"], pages_routes._unique_slug(conn, title), title, "",
                                                  json.dumps(blocks), t, t)).lastrowid
    conn.commit()
    made.insert(0, {"kind": "page", "name": f"Page: {title}", "state": "Draft", "href": url_for("pages.edit", pid=pid)})
    return made, "", fid


def _make_flow(conn, part, form_id) -> dict:
    """The follow-up from its template, switched off, set to start from the campaign's form when there is one."""
    import flows_routes
    t = flows.TEMPLATE_BY_ID[part["template"]]
    tree = flows.from_template(t["id"])
    if tree["trigger"].get("kind") == "form_submitted":
        forms = flows_routes._forms(conn)
        tree["trigger"]["form_id"] = form_id or (forms[0]["id"] if forms else None)
    fid = flows_routes._create(conn, t["name"], tree)
    return {"kind": "flow", "name": f"Follow-up: {t['name']}", "state": "Switched off",
            "href": url_for("flows.edit", fid=fid)}


@bp.route("/app/desk/run", methods=["POST"])
@login_required
def run():
    """Make the ticked parts. `total` must be the total the member was shown; when it isn't, nothing starts (409).
    The ads go first, through Studio (so a step Create asks for, or a refusal, comes back before anything else is
    made); then the copy, the page and its form, and the follow-up."""
    if not ui_v2.on():
        return studio_v2._off()
    d = studio_v2._payload()
    if d.get("total") in (None, ""):
        return jsonify({"error": "See the price first, then confirm it.", "needs_quote": True}), 409
    with db.connect() as conn:
        product = _product(conn, d.get("product"))
    if product is None:
        return jsonify({"error": "Pick a product first."}), 400
    cast = studio_v2.roster()
    q = price(clean_parts(d.get("parts"), cast), product, cast)
    on = [v for v in q["parts"] if v["on"]]
    if not on:
        return jsonify({"error": "Tick at least one part."}), 400
    if str(q["total"]) != str(d.get("total")):
        return jsonify({"error": f"The total changed to {q['total']:,} credits. Check it and confirm again.",
                        "total": q["total"]}), 409
    if q["short"]:
        return jsonify({"error": f"That needs {q['total']:,} credits and you have {q['balance']:,}. Untick a part or "
                                 "top up on Billing.", "billing": url_for("billing_page"), "pack": q["pack"]}), 402
    said = studio_v2._clean(d.get("ask"), 600)
    made, notes, spent = [], [], 0
    ads = [v for v in on if v["kind"] in MAKES]
    if ads:
        body = {"plan": [_make_item(v) for v in ads], "product": product["id"], "ask": said, "total": q["ads"],
                "confirm_many": bool(d.get("confirm_many"))}
        parts = studio_v2.plan_parts(body)
        title, extra = studio_v2._title_for(body, parts)
        out = studio_v2.generate_parts(parts, body, title, extra)
        resp, status = out if isinstance(out, tuple) else (out, 200)
        data = resp.get_json(silent=True) or {}
        if status != 200 or not data.get("ok"):
            return out                                  # nothing has been made: Create's own answer
        n = sum((v.get("count") or 1) if v["kind"] == "image" else 1 for v in ads)
        made.append({"kind": "ads", "name": _plural(n, "ad"), "state": "Being made", "href": data["url"]})
        spent += q["ads"]                               # (a part Create refused is flashed by Studio itself)
    for v in on:
        if v["kind"] == "copy":
            done, why = _write_copy(v, product, said)
            if done:
                made.append(done)
            else:
                notes.append(why)
    with db.connect() as conn:
        form_id = None
        for v in on:
            if v["kind"] == "page":
                done, why, form_id = _make_page(conn, v, product)
                made += done
                if why:
                    notes.append(why)
        for v in on:
            if v["kind"] == "flow":
                made.append(_make_flow(conn, v, form_id))
        if any(v["kind"] == "launch" for v in on):
            made.append({"kind": "launch", "name": "Get the ads into your ad accounts", "state": "Next",
                         "href": url_for(ENDPOINT["launch"])})
        rid = conn.execute("INSERT INTO desk_runs (workspace_id, user_id, product_id, ask, made_json, credits, created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (g.ws["id"], g.user["id"], product["id"], said, json.dumps(made),
                                                      spent, db.now())).lastrowid
        conn.commit()
    for note in notes:
        flash(note)
    return jsonify({"ok": True, "run": rid, "made": made, "notes": notes, "url": url_for("desk.page", done=rid)})


def _runs(conn, limit=RUNS_SHOWN, only=None) -> list[dict]:
    """The campaigns this workspace made from the desk, newest first."""
    sql, args = "SELECT * FROM desk_runs WHERE workspace_id=?", [g.ws["id"]]
    if only:
        sql, args = sql + " AND id=?", args + [only]
    out = []
    for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall():
        try:
            made = [m for m in json.loads(r["made_json"] or "[]") if isinstance(m, dict) and m.get("href")]
        except ValueError:
            made = []
        out.append({"id": r["id"], "ask": r["ask"] or "A campaign", "at": r["created_at"], "made": made})
    return out


def _now(conn) -> list[dict]:
    """What is waiting for the member, each a link: new leads, ads being made, ads finished this week."""
    import datetime
    out = []
    if plans.has(g.ws, "forms") and "forms.leads" in current_app.view_functions:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        st = pipeline.stages(ws)
        known = [s["key"] for s in st]
        marks = ",".join("?" * len(known))
        n = conn.execute(f"SELECT COUNT(*) FROM contacts WHERE workspace_id=? AND (stage IS NULL OR stage=? OR stage NOT IN ({marks}))",
                         (g.ws["id"], known[0], *known)).fetchone()[0]
        if n:
            out.append({"line": f"{_plural(n, 'lead')} in {st[0]['name']}", "href": url_for("forms.leads")})
    busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND status IN ('pending','running')",
                        (g.ws["id"],)).fetchone()[0]
    if busy:
        out.append({"line": f"{_plural(busy, 'ad')} being made", "href": url_for("studio_v2.page")})
    week = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).replace(microsecond=0).isoformat()
    done = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND status='done' AND created_at>=?",
                        (g.ws["id"], week)).fetchone()[0]
    if done:
        out.append({"line": f"{_plural(done, 'ad')} finished this week", "href": url_for("library")
                    if "library" in current_app.view_functions else url_for("studio_v2.page")})
    return out


@bp.route("/app/desk")
@login_required
def page():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import textlayer
    g.v2_tab = "desk"
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        product = _product(conn, ws["product_id"])
        b = products.get_brand(conn, ws["id"], product["brand_id"]) if product is not None else None
        angles = conn.execute("SELECT title FROM angles WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 3",
                              (product["id"], ws["id"])).fetchall() if product is not None else []
        first = starters(conn, product) if product is not None else {}
        runs = _runs(conn)
        done = _runs(conn, 1, request.args.get("done", type=int)) if request.args.get("done") else []
        now = _now(conn)
    brief = products.as_brief(b, product, ws)
    keys = b.keys() if b is not None else []
    kit = [c for c in (textlayer.clean_hex(b[k] or "") if k in keys else "" for k in ("kit_primary", "kit_accent")) if c]
    name = product["name"] if product is not None else ""
    tries = [f"A campaign for {name} that leads with “{a['title']}”" for a in angles]
    tries += [f"Launch {name}: three image ads and one presenter video",
              f"A page to collect emails for {name}, with a follow-up",
              f"Two image ads and ad copy for {name}"][:max(0, 3 - len(tries))]
    have = {m["id"] for m in studio_v2.available_modes()}
    cast = studio_v2.roster()
    boot = {
        "csrf": studio_v2._csrf(), "product": product["id"] if product is not None else None, "balance": db.balance(ws),
        "ask_on": llm.configured(), "starters": first, "max_count": studio_v2.MAX_COUNT,
        "max_makes": studio_v2.ASK_MAX_PARTS,
        "add": [{"kind": k, "name": ADD_NAMES[k], "locked": _locked(k)} for k in (*MAKES, *SPINE)
                if (k in have if k in MAKES else _has(k)) and not (k == "presenter" and not cast)],
        "presenters": [{"id": c["id"], "name": c.get("name") or c["id"].title(),
                        "places": [{"id": pl, "name": studio_v2.situation_labels().get(pl, str(pl).replace("_", " "))}
                                   for pl in studio_v2._places(c)]} for c in cast],
        "platforms": [{"id": k, "name": v["short"]} for k, v in platforms.PLATFORMS.items()],
        "flows": flow_templates(),
        "words": {str(dur): catalog.hard_words(dur) for dur in catalog.TOOLS["ugc_video"]["durations"]},
        "claims_check": studio_v2.claims_check.on() and llm.anthropic_ready(),
        "urls": {"plan": url_for("desk.plan"), "quote": url_for("desk.quote"), "run": url_for("desk.run"),
                 "claims": url_for("studio_v2.claims"), "billing": url_for("billing_page")},
    }
    return render_template("v2/desk.html", boot=boot, product=product, brief=brief, kit=kit, tries=tries, runs=runs,
                           done=done[0] if done else None, now=now, stages=STAGES,
                           website=(brief.get("website") or brief.get("url") or ""))

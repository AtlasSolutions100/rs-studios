"""The setup assistant's pages: /app/setup (full page, first run) and the
"Help me" panel on every screen. JSON endpoints; the conversation is kept per
member so it can be resumed.

Spending happens only in action(): the member confirms a card, and the app
does the thing at the price the card showed. The assistant's own replies are
free up to assistant.free_credits() per workspace, then cost what they use
(x markup) once the member has said that's fine.
"""
import json

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import assistant
import catalog
import compliance
import db
import fetcher
import llm
import product_import
import products
from core import charge_ai, login_required, queue_job

bp = Blueprint("assistant", __name__)
PER_HOUR = 80          # member messages per workspace per hour


def _thread(conn, create=True):
    t = conn.execute("SELECT * FROM assistant_threads WHERE user_id=? AND workspace_id=? AND status='open' "
                     "ORDER BY id DESC LIMIT 1", (g.user["id"], g.ws["id"])).fetchone()
    if t is None and create:
        tid = conn.execute("INSERT INTO assistant_threads (workspace_id, user_id, data_json, created_at, updated_at) "
                           "VALUES (?,?,?,?,?)", (g.ws["id"], g.user["id"], "{}", db.now(), db.now())).lastrowid
        conn.commit()
        t = conn.execute("SELECT * FROM assistant_threads WHERE id=?", (tid,)).fetchone()
    return t


def _data(t):
    try:
        d = json.loads(t["data_json"] or "{}")
    except ValueError:
        d = {}
    if not d.get("setup"):
        try:
            d["setup"] = json.loads(g.ws["setup_json"] or "{}")
        except (ValueError, TypeError):
            d["setup"] = {}
    return d


def _save(conn, t, data, credits=0, free=0, usd=0.0):
    conn.execute("UPDATE assistant_threads SET data_json=?, credits=credits+?, free_credits=free_credits+?, "
                 "cost_usd=cost_usd+?, updated_at=? WHERE id=?",
                 (json.dumps(data), credits, free, round(usd, 5), db.now(), t["id"]))
    conn.execute("UPDATE workspaces SET setup_json=? WHERE id=?", (json.dumps(data.get("setup") or {}), g.ws["id"]))
    conn.commit()


def _free_left(conn):
    used = conn.execute("SELECT assistant_free_used FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()[0] or 0
    return max(0, assistant.free_credits() - used)


def _payload(conn, data, extra=None):
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
    st = assistant.state(conn, ws, data.get("setup") or {})
    out = {"progress": [{"step": s, "done": st["done"][s]} for s in assistant.STEPS + assistant.OPTIONAL],
           "next": st["next"], "free_left": _free_left(conn), "per_reply": assistant.estimate(),
           "paid_ok": bool(data.get("paid_ok")), "balance": db.balance(ws), "ai_on": llm.anthropic_ready()}
    if st.get("nudge"):
        out["nudge"] = dict(st["nudge"], url=url_for(assistant.LINKS[st["nudge"]["target"]]))
    out.update(extra or {})
    return out


def _links(card):
    """Resolve URLs for link cards (done here so the model never makes URLs)."""
    if card.get("kind") == "show_link":
        tgt = card.get("target")
        card["url"] = url_for("products.edit", pid=g.ws["product_id"]) if tgt == "product" and g.ws["product_id"] \
            else url_for(assistant.LINKS.get(tgt, "brand_kit"))
    if card.get("kind") == "propose_first_ad" and card.get("ad") == "ugc":
        card["url"] = url_for("create.index", type="ugc", product=card.get("product"))
    if card.get("kind") == "request_photo":
        card["upload"] = url_for("upload_photos")
        card.update(_photo_choices(card.get("product")))
    return card


def _photo_choices(pid):
    """What the photo card shows in the chat: the product's photos so far and
    the ones the page import found that aren't on it yet (to tick)."""
    if not pid:
        return {"have": [], "found": []}
    with db.connect() as conn:
        p = products.get_product(conn, g.ws["id"], pid)
        if p is None:
            return {"have": [], "found": []}
        have = [url_for("photo", pid=r["id"]) for r in products.photos_for(conn, g.ws["id"], pid)][:8]
    imp = products.import_info(p)
    return {"have": have, "found": [u for u in imp.get("images", []) if u not in imp.get("taken", [])][:12]}


def _items(items):
    for it in items:
        if it.get("role") == "card":
            _links(it["card"])
    return items


@bp.route("/app/setup")
@login_required
def page():
    if not llm.anthropic_ready():
        flash("The setup guide isn't switched on yet. Here's the quick way to your first ad.")
        return redirect(url_for("studio"))
    return render_template("setup.html")


@bp.route("/app/assistant")
@login_required
def thread():
    with db.connect() as conn:
        t = _thread(conn)
        data = _data(t)
        return jsonify(_payload(conn, data, {"items": _items(assistant.display(data))}))


@bp.route("/app/assistant/reset", methods=["POST"])
@login_required
def reset():
    with db.connect() as conn:
        conn.execute("UPDATE assistant_threads SET status='closed' WHERE user_id=? AND workspace_id=? AND status='open'",
                     (g.user["id"], g.ws["id"]))
        conn.commit()
        data = _data(_thread(conn))
        return jsonify(_payload(conn, data, {"items": []}))


@bp.route("/app/assistant/ok", methods=["POST"])
@login_required
def ok_paid():
    """The member agreed that replies past the free allowance cost credits."""
    with db.connect() as conn:
        t = _thread(conn)
        data = _data(t)
        data["paid_ok"] = True
        _save(conn, t, data)
        return jsonify(_payload(conn, data))


def _turn(conn, t, data, screen, job_started=False):
    """Run the assistant on the conversation as it stands; charge for it."""
    free_left = _free_left(conn)
    est = assistant.estimate()
    if free_left <= 0 and not data.get("paid_ok"):
        return None, {"need_ok": True}
    if free_left <= 0 and db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()) < est:
        return None, {"error": f"Replies cost about {est} credits now and you have {db.balance(g.ws):,}. Top up on Billing."}
    usage = llm.Usage()
    err = None
    try:
        shown = assistant.run(conn, g.ws, data, screen, usage, job_started=job_started)
    except llm.LLMError as e:
        shown, err = [], f"The guide didn't answer ({e}). Try again in a moment."
    credits = usage.credits if usage.calls else 0
    free = min(credits, free_left)
    paid = charge_ai(conn, credits - free, "AI setup guide") if credits - free > 0 else 0
    if free:
        conn.execute("UPDATE workspaces SET assistant_free_used=assistant_free_used+? WHERE id=?", (free, g.ws["id"]))
    _save(conn, t, data, paid, free, usage.usd)
    return shown, {"charged": paid, **({"error": err} if err else {})}


@bp.route("/app/assistant/say", methods=["POST"])
@login_required
def say():
    d = request.get_json(silent=True) or {}
    text = " ".join(str(d.get("text") or "").split())[:1500]
    screen = str(d.get("screen") or "")[:80]
    if not llm.anthropic_ready():
        return jsonify({"error": "The setup guide isn't switched on yet."}), 503
    key = f"assist:{g.ws['id']}"
    if db.throttled(key, PER_HOUR, 3600):
        return jsonify({"error": "That's a lot of messages this hour. Take a break and come back."}), 429
    with db.connect() as conn:
        t = _thread(conn)
        data = _data(t)
        if d.get("start"):
            if data.get("messages"):
                return jsonify(_payload(conn, data, {"items": []}))
            text = f"{assistant.KICKOFF}{' on ' + screen if screen else ''}. Greet them in one line and start."
        elif not text:
            abort(400)
        free_left = _free_left(conn)
        if free_left <= 0 and not data.get("paid_ok"):
            return jsonify(_payload(conn, data, {"need_ok": True, "items": []}))
        db.hit(key)
        card = None if d.get("start") else assistant.confirmable_card(data)
        if card and assistant.is_confirm(text):
            # A typed "yes" / "confirmed" while a spend card is open is the same
            # as pressing its button: the app does it, at the card's price.
            return _confirm_by_text(conn, t, data, card, text, screen)
        assistant.add_member(data, text)
        shown, extra = _turn(conn, t, data, screen)
        if shown is None:
            data["messages"].pop()
            return jsonify(_payload(conn, data, dict(extra, items=[])))
        mine = [] if d.get("start") else [{"role": "user", "text": text}]
        return jsonify(_payload(conn, data, dict(extra, items=_items(mine + shown))))


def _confirm_by_text(conn, t, data, card, text, screen):
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
    if card["kind"] == "propose_import":
        note, extra = _do_import(conn, card)
    else:
        note, extra = _do_first_ad(conn, ws, card)
    data["cards"][card["id"]] = card
    assistant.add_member(data, text)
    if note is None:        # e.g. not enough credits for the import: nothing ran
        assistant.add_member(data, "(app) The seller typed yes, but nothing ran: " + (extra.get("error") or "unknown"))
        _save(conn, t, data)
        return jsonify(_payload(conn, data, dict(extra, items=_items([{"role": "user", "text": text}]))))
    assistant.add_member(data, "(app) The seller typed yes, so the app pressed the card's button. " + note)
    _save(conn, t, data)
    shown, more = _turn(conn, t, data, screen, job_started=bool(extra.get("job")))
    items = [{"role": "user", "text": text}, {"role": "card", "card": card}, {"role": "note", "text": note}] + (shown or [])
    return jsonify(_payload(conn, data, dict(extra, **more, items=_items(items), card=_links(card))))


@bp.route("/app/assistant/action", methods=["POST"])
@login_required
def action():
    """The member answered a card. Spending happens here, at the card's price."""
    d = request.get_json(silent=True) or {}
    with db.connect() as conn:
        t = _thread(conn)
        data = _data(t)
        card = (data.get("cards") or {}).get(str(d.get("card") or ""))
        if not card:
            abort(404)
        if card.get("status") != "open":
            return jsonify(_payload(conn, data, {"items": [], "error": "That card was already answered."}))
        choice = d.get("choice")
        note, extra = None, {}
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
        if choice == "cancel":
            card["status"] = "cancelled"
            note = {"propose_import": "The seller didn't import the page.", "propose_first_ad": "The seller said not now to the first ad.",
                    "review_suggested_claims": "The seller approved none of the suggested claims.",
                    "request_photo": "The seller skipped photos for now."}.get(card["kind"], "The seller dismissed the card.")
        elif card["kind"] == "propose_import":
            note, extra = _do_import(conn, card)
        elif card["kind"] == "review_suggested_claims":
            p = products.get_product(conn, g.ws["id"], card.get("product"))
            if p is None:
                abort(404)
            picks = [int(i) for i in d.get("picks") or [] if str(i).isdigit()]
            pend = products.pending(p)
            chosen = [pend[i] for i in picks if 0 <= i < len(pend)]
            try:
                took = products.approve_claims(conn, g.ws["id"], p["id"], [pend.index(c) for c in chosen])
            except products.ProductError as e:
                return jsonify(_payload(conn, data, {"items": [], "error": str(e)}))
            card["status"] = "done"
            card["approved"] = took
            note = f"The seller approved {len(took)} claim(s): " + "; ".join(took) if took else \
                "The seller approved none of the suggested claims."
        elif card["kind"] == "request_photo":
            picks = [str(u) for u in d.get("picks") or []][:10]
            if picks:
                import product_routes
                p = products.get_product(conn, g.ws["id"], card.get("product"))
                if p is None:
                    abort(404)
                added, failed = product_routes.add_candidate_photos(conn, g.ws["id"], p, picks)
                if not added:
                    return jsonify(_payload(conn, data, {"items": [], "error": "Those photos couldn't be downloaded. "
                                                         "Upload your own below instead."}))
            n = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (card.get("product"),)).fetchone()[0]
            card["status"] = "done" if n else "open"
            note = f"The seller added photos; the product has {n} photo(s) now." if n else None
            if not n:
                return jsonify(_payload(conn, data, {"items": [], "error": "No photos arrived. Try the upload again."}))
        elif card["kind"] == "propose_first_ad" and card.get("ad") == "image":
            note, extra = _do_first_ad(conn, ws, card)
        else:
            card["status"] = "done"
            note = "The seller opened it."
        data["cards"][card["id"]] = card
        if note is None:
            _save(conn, t, data)
            return jsonify(_payload(conn, data, dict(extra, items=[])))
        assistant.add_member(data, "(app) " + note)
        _save(conn, t, data)
        shown, more = _turn(conn, t, data, str(d.get("screen") or "")[:80], job_started=bool(extra.get("job")))
        items = [{"role": "note", "text": note}] + (shown or [])
        return jsonify(_payload(conn, data, dict(extra, **more, items=_items(items), card=_links(card))))


def _do_import(conn, card):
    import product_routes
    quote = product_import.estimate() if llm.anthropic_ready() else 0
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
    if quote and db.balance(ws) < quote:
        return None, {"error": f"Reading the page needs about {quote} credits and you have {db.balance(ws):,}."}
    try:
        pid, facts, charged = product_routes.import_url(conn, card["url"], ws["brand_id"])
        products.activate(conn, ws, product_id=pid)
    except (fetcher.FetchError, products.ProductError) as e:
        card["status"] = "failed"
        card["error"] = str(e)
        return f"The import didn't work: {e}", {}
    p = products.get_product(conn, g.ws["id"], pid)
    card.update(status="done", product=pid, product_name=p["name"], charged=charged)
    g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
    return (f"Imported '{p['name']}' from the page ({charged} credits): price {p['price'] or 'not found'}, "
            f"{len(products.pending(p))} suggested claim(s) waiting for approval, "
            f"{len(products.import_info(p).get('images') or [])} candidate photo(s) on its product page."), {"import_charged": charged}


def _do_first_ad(conn, ws, card):
    p = products.get_product(conn, g.ws["id"], card.get("product"))
    if p is None:
        abort(404)
    tpl = catalog.TEMPLATE_BY_ID.get(card.get("template")) or catalog.first_run_templates()[0]
    b = products.brief_for(conn, ws, p["id"])
    brief = catalog.fill(tpl["brief"], b)
    if not compliance.check(brief, b.get("audience") or "")["ok"]:
        return "That ad's brief broke the content rules, so nothing was made.", {}
    photo_ids = [x["id"] for x in products.photos_for(conn, g.ws["id"], p["id"])][:3]
    price = catalog.price_credits("ad_image", batch=1)
    if price != card.get("credits"):
        return f"The price changed to {price} credits; nothing was made. Offer it again.", {}
    params = {"photo_ids": photo_ids, "batch": 1, "aspect": tpl.get("aspect") or "9:16",
              "audience": (b.get("audience") or tpl.get("audience") or "")[:200], "niche": ""}
    jid = queue_job(conn, "ad_image", tpl["id"], brief, params, catalog.cost_usd("ad_image", batch=1), price, quiet=True,
                    product_id=p["id"])
    if not jid:
        return f"Not enough credits for the ad ({price} needed); nothing was made.", {}
    card.update(status="done", job=jid, library=url_for("library", highlight=jid))
    return (f"The first ad is being made: a {tpl['name']} image ad of {p['name']} for {price} credits. It shows up in "
            "the library in a minute or two."), {"job": jid}

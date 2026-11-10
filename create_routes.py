"""Create: product -> ad type -> angle -> script -> presenter and voice ->
format -> quote -> generate -> review. The heart of the app.

The page is one guided flow (web/create.html) that talks to the JSON
endpoints here. Nothing is generated until /app/create/generate, which
recomputes the price from the same config and refuses if the total the member
saw has changed. Angle and script writing are small AI calls charged at cost.
"""
import json

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

import adfatigue
import admanage
import catalog
import claims_check
import compliance
import creator
import credits as credit_holds
import db
import llm
import platforms
import presenter_stills
import presenters
import presets
import products
from core import admin_required, brand, hold_check, login_required, queue_job, reserve_ai, verify_block, verify_json

bp = Blueprint("create", __name__)
MAX_BATCH = 12
MAX_ITEMS, MAX_PRESENTERS, MAX_ASPECTS = 5, 4, 3
PLATFORM_ASPECTS = {"tiktok": "9:16", "meta": "4:5", "youtube": "16:9", "snapchat": "9:16", "pinterest": "9:16",
                    "x": "1:1", "linkedin": "1:1", "reddit": "1:1"}


VIDEO_TOOLS = ("ugc_video", "photo_to_video", "motion_copy")
WARN_VIDEOS, CONFIRM_VIDEOS = 4, 8   # warn above 4 videos; above 8 the member ticks a second confirmation


class ConfigError(ValueError):
    """A config the flow can't make. `step` is where the member fixes it."""

    def __init__(self, msg, step=""):
        super().__init__(msg)
        self.step = step


def suggest_pack(short: int):
    """The cheapest credit pack that covers a shortfall (or the biggest one)."""
    if short <= 0:
        return None
    packs = sorted(catalog.PACKS, key=lambda p: p["usd"])
    p = next((p for p in packs if p["credits"] >= short), packs[-1])
    return {"code": p["code"], "name": p["name"], "credits": p["credits"], "usd": p["usd"], "covers": p["credits"] >= short}


def _json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


def _product_row(conn, pid):
    p = products.get_product(conn, g.ws["id"], pid) if pid else None
    if p is None or p["brand_id"] not in products.unlocked_brand_ids(conn, g.ws):
        return None
    return p


def state_for(conn, pid):
    """What the page needs about one product: photos, saved angles/scripts."""
    p = _product_row(conn, pid)
    if p is None:
        return None
    photos = products.photos_for(conn, g.ws["id"], p["id"])
    angles = conn.execute("SELECT * FROM angles WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 20",
                          (p["id"], g.ws["id"])).fetchall()
    scripts = conn.execute("SELECT * FROM scripts WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 20",
                           (p["id"], g.ws["id"])).fetchall()
    return {"id": p["id"], "name": p["name"], "brand_id": p["brand_id"], "price": p["price"] or "",
            "claims": p["claims"] or "", "audience": p["audience"] or "",
            "photos": [{"id": x["id"], "url": url_for("photo", pid=x["id"])} for x in photos],
            "angles": [{"id": a["id"], "title": a["title"], "hook": a["hook"] or "", "insight": a["insight"] or "",
                        "audience": a["audience"] or ""} for a in angles],
            "scripts": [{"id": s["id"], "ad_type": s["ad_type"], "title": s["title"] or "", "hook": s["hook"] or "",
                         "body": s["body"], "items": json.loads(s["on_screen"] or "{}").get("items") or
                         ([s["hook"]] if s["hook"] else [])} for s in scripts],
            "offer": p["offer"] or "",
            # the presets that fit this product, best fit first (its category's own, then the any-product ones)
            "templates": [x.id for x in presets.library_for(p)]}


@bp.route("/app/create")
@login_required
def index():
    """The guided flow. ?product= & ?type= preselect; ?from_ad= hands off a
    planner proposal; ?batch= starts from an earlier batch."""
    with db.connect() as conn:
        pid = request.args.get("product", type=int) or g.ws["product_id"]
        prefill = {}
        if request.args.get("from_ad", type=int):
            prefill = _from_planner(conn, request.args.get("from_ad", type=int))
            pid = prefill.get("product") or pid
        elif request.args.get("batch", type=int):
            b = conn.execute("SELECT * FROM batches WHERE id=? AND workspace_id=?",
                             (request.args.get("batch", type=int), g.ws["id"])).fetchone()
            if b:
                if not request.args.get("rest"):     # "Make more like this" (not "Make the rest" of the same batch)
                    import site_events
                    site_events.on_asset(conn, "more_like_this", conn.execute(
                        "SELECT * FROM jobs WHERE batch_id=? AND workspace_id=? ORDER BY id LIMIT 1",
                        (b["id"], g.ws["id"])).fetchone(), request)
                prefill = dict(json.loads(b["config_json"] or "{}"), step="script")
                pid = b["product_id"] or pid
                if request.args.get("rest") and prefill.get("made"):
                    # "Make the rest": the same config, at review, with the lines already made unticked.
                    prefill.update(step="review", skip=prefill["made"], rest_of=b["id"])
                prefill.pop("made", None)
        refresh = None
        if request.args.get("refresh") and admanage.on(g.ws, g.get("user")):
            # "Refresh this ad" from a worn-out launched ad (adfatigue.py, M4.2): its format, the winning angle
            r = adfatigue.refresh_prefill(conn, g.ws["id"], request.args.get("refresh") or "")
            if r is not None:
                prefill, refresh = r["prefill"], r["note"]
                pid = prefill.get("product") or pid
        if request.args.get("retry", type=int):
            prefill = _from_job(conn, request.args.get("retry", type=int)) or prefill
            pid = prefill.get("product") or pid
        tpl = catalog.TEMPLATE_BY_ID.get(request.args.get("template") or "")
        if tpl and tpl.get("layout") and not presets.text_layers_on():
            tpl = None
        if tpl and tpl.get("recipe") and not presets.runnable(presets.get(tpl["id"])):
            tpl = None          # a video preset that waits for the keyframe-first pipeline can't be started from a link
        if tpl and not prefill:
            prefill = catalog.template_prefill(tpl, brand(pid if _product_row(conn, pid) is not None else None))
        if request.args.get("type") in catalog.AD_TYPE_BY_ID:
            prefill.setdefault("type", request.args["type"])
        if request.args.get("angle", type=int):
            a = conn.execute("SELECT * FROM angles WHERE id=? AND workspace_id=?",
                             (request.args.get("angle", type=int), g.ws["id"])).fetchone()
            if a:
                pid = a["product_id"]
                prefill["angle"] = {"title": a["title"], "hook": a["hook"] or "", "insight": a["insight"] or "",
                                    "audience": a["audience"] or ""}
        st = state_for(conn, pid)
        tree = products.tree(conn, g.ws)
        refs = conn.execute("SELECT id, title, seconds FROM refs WHERE active=1 ORDER BY id DESC").fetchall()
    prices = {"ugc_video": {d: catalog.price_credits("ugc_video", duration=d) for d in catalog.TOOLS["ugc_video"]["durations"]},
              "photo_to_video": {d: catalog.price_credits("photo_to_video", duration=d) for d in (5, 10)},
              "ugc_video_1080p": {d: catalog.price_credits("ugc_video", duration=d, resolution="1080p")
                                  for d in catalog.TOOLS["ugc_video"]["durations"]},
              "photo_to_video_1080p": {d: catalog.price_credits("photo_to_video", duration=d, resolution="1080p")
                                       for d in (5, 10)},
              "ad_image": {n: catalog.price_credits("ad_image", batch=n) for n in (1, 2, 3, 4)},
              "motion_copy": {r["id"]: catalog.price_credits("motion_copy", ref_seconds=r["seconds"] or 15) for r in refs}}
    boot = {"product": st, "prefill": prefill, "types": catalog.ad_types(), "caps": catalog.capabilities(),
            "presenters": presenter_stills.with_photos(url_for), "voices": catalog.VOICES, "prices": prices,
            "from_price": catalog.from_prices(),
            # the preset library (presets.py): every template and static, each with its one-line "what it's for"
            # and a thumbnail; the page lists the ones that fit the product picked (product.templates)
            "templates": [presets.card(x, url_for) for x in presets.OLD_TEMPLATES + presets.SCENES
                          + (presets.layered() if presets.text_layers_on() else [])],
            "layouts": {x.id: presets.layout_spec(x) for x in presets.layered()} if presets.text_layers_on() else {},
            # the claims check costs a member nothing (m15: we absorb about $0.004 a script)
            "claims_check": claims_check.on() and llm.anthropic_ready(), "quote_claims": claims_check.estimate(),
            "refs": [{"id": r["id"], "title": r["title"], "seconds": r["seconds"], "url": url_for("ref_video", rid=r["id"])}
                     for r in refs],
            "durations": {"ugc_video": catalog.TOOLS["ugc_video"]["durations"], "photo_to_video": [5, 10]},
            "aspects": {"ugc_video": catalog.TOOLS["ugc_video"]["aspects"], "ad_image": catalog.TOOLS["ad_image"]["aspects"]},
            "platforms": {k: platforms.PLATFORMS[k]["short"] for k in PLATFORM_ASPECTS if k in platforms.PLATFORMS},
            "platform_aspects": PLATFORM_ASPECTS, "wps": catalog.WORDS_PER_SECOND,
            "budget": {d: catalog.word_budget(d) for d in catalog.TOOLS["ugc_video"]["durations"]},
            "quote_shorten": creator.estimate_shorten(),
            "ai": llm.anthropic_ready(), "quote_angles": creator.estimate_angles(), "quote_script": max(creator.estimate_script(), creator.estimate_script(False, True)),
            "max_batch": MAX_BATCH, "max_items": MAX_ITEMS, "max_presenters": MAX_PRESENTERS,
            "warn_videos": WARN_VIDEOS, "confirm_videos": CONFIRM_VIDEOS, "balance": db.balance(g.ws),
            "urls": {"angles": url_for("create.angles"), "script": url_for("create.script"), "save": url_for("create.save"),
                     "quote": url_for("create.quote"), "generate": url_for("create.generate"),
                     "shorten": url_for("create.shorten"),
                     "check": url_for("create.check_step"), "billing": url_for("billing_page"),
                     "claims": url_for("create.claims"),
                     "state": url_for("create.product_state"), "new_product": url_for("products.new"),
                     "edit_product": url_for("products.edit", pid=0)}}
    return render_template("create.html", boot=boot, tree=tree, refresh=refresh)


def _from_job(conn, jid):
    """Edit & retry: the batch's settings narrowed to this one ad (its hook,
    presenter or voice, and shape), opened at the script step."""
    j = conn.execute("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (jid, g.ws["id"])).fetchone()
    if not j or not j["batch_id"]:
        return {}
    b = conn.execute("SELECT * FROM batches WHERE id=? AND workspace_id=?", (j["batch_id"], g.ws["id"])).fetchone()
    if not b:
        return {}
    cfg = json.loads(b["config_json"] or "{}")
    cfg.pop("made", None)
    cfg.pop("every", None)
    p = json.loads(j["params_json"] or "{}")
    if p.get("hook") and cfg.get("items"):
        cfg["items"] = [p["hook"]]
    if p.get("presenter"):
        cfg["presenters"] = [p["presenter"]]
    elif p.get("voice") and cfg.get("type") == "voiceover":
        cfg["voices"] = [p["voice"]]
    if p.get("aspect"):
        cfg["aspects"] = [p["aspect"]]
    cfg.update(step="script", product=b["product_id"], retry_of=j["id"])
    return cfg


def _from_planner(conn, aid):
    """A planner proposal as a starting point for the flow."""
    a = conn.execute("SELECT * FROM campaign_ads WHERE id=? AND workspace_id=?", (aid, g.ws["id"])).fetchone()
    if not a:
        return {}
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (a["campaign_id"],)).fetchone()
    plan = json.loads(c["plan_json"] or "{}") if c else {}
    ang = next((x for x in plan.get("angles") or [] if x.get("name") == a["angle"]), {}) or {}
    cr = next((x for x in plan.get("creative") or [] if x.get("angle") == a["angle"]), {}) or {}
    params = json.loads(a["params_json"] or "{}")
    typ = params.get("ad_type") or catalog.TOOL_AD_TYPE.get(a["tool"], "image")
    kind = (catalog.AD_TYPE_BY_ID.get(typ) or {}).get("script")
    out = {"type": typ, "product": c["product_id"] if c else None, "from_ad": aid, "campaign": a["campaign_id"],
           "angle": {"title": a["angle"] or ang.get("name") or "", "hook": (cr.get("hooks") or [""])[0],
                     "insight": ang.get("insight") or "", "audience": ang.get("audience") or ""},
           "platforms": [a["platform"]] if a["platform"] in PLATFORM_ASPECTS else [], "step": "script"}
    if kind == "spoken":
        out["items"], out["body"] = [a["brief"] or ""], ""
        if params.get("presenter"):
            out["presenters"] = [params["presenter"]]
    elif kind == "image":
        out["items"] = (cr.get("on_screen_text") or [])[:3] or (cr.get("hooks") or [])[:3]
        out["body"] = a["brief"] or ""
    else:
        out["items"] = [a["brief"] or ""]
    if params.get("aspect"):
        out["aspects"] = [params["aspect"]]
    if params.get("duration"):
        out["duration"] = params["duration"]
    return out


@bp.route("/app/create/product")
@login_required
def product_state():
    with db.connect() as conn:
        st = state_for(conn, request.args.get("id", type=int))
    if st is None:
        abort(404)
    return jsonify(st)


def _ai_hold(quote, label):
    """(hold, None) with the most the call can cost held (credits.py), or (None, the 503/402 answer)."""
    if not llm.anthropic_ready():
        return None, (jsonify({"error": "AI writing isn't switched on yet. Write your own below; nothing was charged."}),
                      503)
    hold = reserve_ai(quote, label)
    if hold is None:
        return None, (jsonify({"error": f"That needs about {quote} credits and you have {_bal():,}."}), 402)
    return hold, None


def _settle(hold, usage):
    try:
        return credit_holds.settle(hold, usage.credits if usage.calls else 0)
    except Exception:  # noqa: BLE001 — never lose the answer over the bookkeeping; the sweep returns the hold
        import logging
        logging.getLogger("campaignify").exception("settling credit hold %s failed", hold)
        return 0


@bp.route("/app/create/angles", methods=["POST"])
@login_required
def angles():
    d = _json()
    with db.connect() as conn:
        p = _product_row(conn, d.get("product"))
        if p is None:
            abort(404)
        hold, busy = _ai_hold(creator.estimate_angles(), "AI angle ideas")
        if busy:
            return busy
        usage = llm.Usage()
        try:
            out = creator.angles(brand(p["id"]), d.get("type") or "ugc", 5, [str(x)[:80] for x in d.get("avoid") or []],
                                 usage)
            err = None
        except llm.LLMError as e:
            out, err = [], f"The writer didn't finish ({e}). Try again."
        taken = _settle(hold, usage)
        conn.commit()
    return jsonify({"angles": out, "charged": taken, "balance": _bal(), **({"error": err} if err else {})})


@bp.route("/app/create/script", methods=["POST"])
@login_required
def script():
    d = _json()
    typ = d.get("type") if d.get("type") in catalog.AD_TYPE_BY_ID else "ugc"
    angle = {k: str((d.get("angle") or {}).get(k) or "")[:240] for k in ("title", "hook", "insight", "audience")}
    if not angle["title"] and not angle["hook"]:
        return jsonify({"error": "Pick or write an angle first."}), 400
    if not compliance.check(*angle.values())["ok"]:
        return jsonify({"error": "That angle breaks the rules: " + " ".join(compliance.check(*angle.values())["blocks"])}), 400
    if catalog.AD_TYPE_BY_ID[typ]["script"] == "spoken":
        duration = _spoken_duration(d)
        if duration is None:
            return jsonify({"error": "Pick a length first."}), 400
    else:
        duration = int(d.get("duration") or 10) if str(d.get("duration") or "10").isdigit() else 10
    with db.connect() as conn:
        p = _product_row(conn, d.get("product"))
        if p is None:
            abort(404)
        spoken = catalog.AD_TYPE_BY_ID[typ]["script"] == "spoken"
        hold, busy = _ai_hold(creator.estimate_script(not spoken, spoken), "AI script")
        if busy:
            return busy
        usage = llm.Usage()
        photo = None
        if catalog.AD_TYPE_BY_ID[typ]["script"] != "spoken":
            ph = _writer_photo(conn, p["id"], d.get("photos"))
            photo = creator.photo_for_writer(ph["path"]) if ph is not None else None
        try:
            out, err = creator.script(brand(p["id"]), typ, angle, duration, max(1, min(MAX_ITEMS, int(d.get("n") or 3))),
                                      usage, photo=photo), None
        except llm.LLMError as e:
            out, err = {"items": [], "body": "", "on_screen": []}, f"The writer didn't finish ({e}). Try again."
        taken = _settle(hold, usage)
        conn.commit()
    return jsonify(dict(out, charged=taken, balance=_bal(), **({"error": err} if err else {})))


def _spoken_duration(d):
    """The talking-video length asked for, if it's one we offer; else None."""
    try:
        dur = int(d.get("duration") or 10)
    except (TypeError, ValueError):
        return None
    return dur if dur in catalog.TOOLS["ugc_video"]["durations"] else None


@bp.route("/app/create/shorten", methods=["POST"])
@login_required
def shorten():
    """"Shorten to fit": the AI cuts a typed spoken script to the length picked,
    keeping one hook per hook typed. Charged at cost; free when it already fits."""
    d = _json()
    typ = d.get("type") if d.get("type") in catalog.AD_TYPE_BY_ID else "ugc"
    if catalog.AD_TYPE_BY_ID[typ]["script"] != "spoken":
        return jsonify({"error": "Only spoken scripts have a length to fit."}), 400
    dur = _spoken_duration(d)
    if dur is None:
        return jsonify({"error": "Pick a length first."}), 400
    items = [" ".join(str(x).split())[:200] for x in (d.get("items") or [])[:MAX_ITEMS] if str(x).strip()]
    body = " ".join(str(d.get("body") or "").split())[:2000]
    if not items and not body:
        return jsonify({"error": "Write a script first."}), 400
    with db.connect() as conn:
        p = _product_row(conn, d.get("product"))
        if p is None:
            abort(404)
        b = catalog.word_budget(dur)
        if all(catalog.words(i + " " + body) <= b["total"] for i in items) and (items or catalog.words(body) <= b["total"]):
            return jsonify({"items": items, "body": body, "changed": False, "charged": 0, "balance": _bal()})
        hold, busy = _ai_hold(creator.estimate_shorten(), "AI shorten script")
        if busy:
            return busy
        usage = llm.Usage()
        try:
            out, err = creator.shorten(brand(p["id"]), typ, items, body, dur, usage), None
        except llm.LLMError as e:
            out, err = None, f"The writer didn't finish ({e}). Try again; nothing was changed."
        taken = _settle(hold, usage)
        conn.commit()
    if out is None:
        return jsonify({"error": err, "charged": taken, "balance": _bal()}), 502
    return jsonify(dict(out, charged=taken, balance=_bal()))


def _writer_photo(conn, pid, picked):
    """The product photo the writer looks at: the first one picked in the
    form if it belongs to this workspace, else the product's primary photo."""
    rows = products.photos_for(conn, g.ws["id"], pid, everything=True)
    by_id = {r["id"]: r for r in rows}
    for x in picked or []:
        if str(x).isdigit() and int(x) in by_id:
            return by_id[int(x)]
    own = [r for r in rows if r["product_id"] == pid]
    return own[0] if own else None


def _bal():
    with db.connect() as conn:
        return db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone())


def save_angle(conn, pid, angle, source="user"):
    title = str(angle.get("title") or angle.get("hook") or "").strip()[:80]
    if not title:
        return None
    hook = str(angle.get("hook") or "").strip()[:240]
    if not compliance.check(title, hook, str(angle.get("insight") or ""))["ok"]:
        return None
    have = conn.execute("SELECT id FROM angles WHERE product_id=? AND title=? AND COALESCE(hook,'')=?",
                        (pid, title, hook)).fetchone()
    if have:
        return have["id"]
    aid = conn.execute("INSERT INTO angles (workspace_id, product_id, title, hook, insight, audience, source, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (g.ws["id"], pid, title, hook, str(angle.get("insight") or "")[:300],
                                                    str(angle.get("audience") or "")[:200], source, db.now())).lastrowid
    conn.commit()
    return aid


def save_script(conn, pid, typ, items, body, title="", angle_id=None, source="user"):
    items = [i for i in (str(x).strip()[:900] for x in items or []) if i][:MAX_ITEMS]
    body = str(body or "").strip()[:2000]
    if not items and not body:
        return None
    if not compliance.check(body, *items)["ok"]:
        return None
    text = body or items[0]
    have = conn.execute("SELECT id FROM scripts WHERE product_id=? AND ad_type=? AND body=? AND COALESCE(hook,'')=?",
                        (pid, typ, text, items[0] if items else "")).fetchone()
    if have:
        return have["id"]
    t = db.now()
    sid = conn.execute("INSERT INTO scripts (workspace_id, product_id, angle_id, ad_type, title, hook, body, on_screen, "
                       "source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (g.ws["id"], pid, angle_id, typ, str(title or "")[:80], items[0] if items else "", text,
                        json.dumps({"items": items}), source, t, t)).lastrowid
    conn.commit()
    return sid


@bp.route("/app/create/save", methods=["POST"])
@login_required
def save():
    d = _json()
    with db.connect() as conn:
        p = _product_row(conn, d.get("product"))
        if p is None:
            abort(404)
        if d.get("what") == "angle":
            sid = save_angle(conn, p["id"], d.get("angle") or {})
        else:
            typ = d.get("type") if d.get("type") in catalog.AD_TYPE_BY_ID else "ugc"
            aid = save_angle(conn, p["id"], d.get("angle") or {}) if (d.get("angle") or {}).get("title") else None
            sid = save_script(conn, p["id"], typ, d.get("items"), d.get("body"), (d.get("angle") or {}).get("title"), aid)
    if not sid:
        return jsonify({"error": "Nothing to save, or it breaks the rules."}), 400
    return jsonify({"ok": True, "id": sid})


# ---------------------------------------------------------------- config -> jobs

def expand(conn, d) -> tuple[dict, list[dict]]:
    """Validate a flow config and turn it into the jobs it would make:
    [{tool, brief, params, credits, usd, label}]. Raises ConfigError."""
    typ = d.get("type")
    at = next((a for a in catalog.ad_types() if a["id"] == typ), None)
    if not at:
        raise ConfigError("Pick an ad type.", "type")
    p = _product_row(conn, d.get("product"))
    if p is None:
        raise ConfigError("Pick a product.", "product")
    tool, kind = at["tool"], at["script"]
    owned = {x["id"] for x in products.photos_for(conn, g.ws["id"], p["id"], everything=True)}
    photo_ids = [int(x) for x in d.get("photos") or [] if str(x).isdigit() and int(x) in owned]
    if not photo_ids:
        photo_ids = [x["id"] for x in products.photos_for(conn, g.ws["id"], p["id"])]
    photo_ids = photo_ids[: catalog.TOOLS[tool]["max_photos"]]
    if tool != "ad_image" and not photo_ids:
        raise ConfigError(f"{p['name']} needs a photo first: videos start from one.", "format")
    items = [" ".join(str(x).split())[:900] for x in d.get("items") or [] if str(x).strip()]
    if len(items) > MAX_ITEMS:
        raise ConfigError(f"Use at most {MAX_ITEMS} hooks, headlines or scenes in one batch.", "script")
    body = " ".join(str(d.get("body") or "").split())[:2000]
    if not items and body:
        items, body = [body], ""
    if not items:
        raise ConfigError("Write or pick at least one hook, headline or scene.", "script")
    b = brand(p["id"])
    jobs = []
    res = str(d.get("resolution") or catalog.DEFAULT_RESOLUTION)
    if catalog.resolutions(tool):
        if res not in catalog.resolutions(tool):
            raise ConfigError("Pick 720p or 1080p.", "format")
    else:
        res = ""
    hd = " · 1080p" if res == "1080p" else ""
    aspects_ok = catalog.TOOLS[tool].get("aspects") or []
    aspects = list(dict.fromkeys(a for a in d.get("aspects") or [] if a in aspects_ok))[:MAX_ASPECTS] or \
        (aspects_ok[:1] if aspects_ok else [None])
    base = {"photo_ids": photo_ids, "ad_type": typ, "product": p["name"]}
    if kind == "spoken":
        # A fixed video format (presets.RECIPES): it owns the shot, the place and the length it starts at.
        rec = presets.get(d.get("preset")) if d.get("preset") else None
        if d.get("preset") and (rec is None or rec.kind != "recipe"):
            raise ConfigError("That preset isn't a video preset. Pick another, or none.", "type")
        if rec is not None and rec.ad_type != typ:
            raise ConfigError(f"{rec.name} is a {'presenter video' if rec.ad_type == 'ugc' else 'voiceover'} preset. "
                              "Pick that ad type for it.", "type")
        if rec is not None and not presets.runnable(rec):
            raise ConfigError(f"{rec.name} isn't available yet. Pick another preset.", "type")
        try:
            dur = int(d.get("duration") or (rec.price.get("duration") if rec is not None else 0) or 10)
        except (TypeError, ValueError):
            dur = 0
        if dur not in catalog.TOOLS[tool]["durations"]:
            raise ConfigError("Pick a length.", "script")
        sit_in = d.get("situation")

        def place(who):
            """The situation of one presenter: the preset's own, else the one picked (one for all, or per presenter)."""
            s = (rec.extra.get("situation") if rec is not None else "") or \
                (sit_in.get(who) if isinstance(sit_in, dict) else sit_in) or ""
            if s and str(s) not in presenters.SITUATIONS:
                raise ConfigError("Pick one of the places on the list.", "cast")
            return str(s)
        limit = catalog.hard_words(dur)
        if typ == "ugc":
            cast = list(dict.fromkeys(x for x in d.get("presenters") or [] if x in catalog.PRESENTER_BY_ID))
            if not cast:
                raise ConfigError("Pick at least one presenter.", "cast")
        else:
            cast = list(dict.fromkeys(x for x in d.get("voices") or [] if x in catalog.VOICE_BY_ID)) or ["narrator_deep"]
        if len(cast) > MAX_PRESENTERS:
            raise ConfigError(f"Pick at most {MAX_PRESENTERS} {'presenters' if typ == 'ugc' else 'voices'} per batch.", "cast")
        voice = d.get("voice") if d.get("voice") in catalog.VOICE_BY_ID else ""
        credits, usd = catalog.price_credits(tool, duration=dur, resolution=res), catalog.cost_usd(tool, duration=dur,
                                                                                                    resolution=res)
        for i, item in enumerate(items):
            said = (item + " " + body).strip()
            if len(said.split()) > limit:
                fit = catalog.fits_duration(len(said.split()))
                raise ConfigError(f"Script {i + 1} is {len(said.split())} words; a {dur}-second video fits about "
                                  f"{catalog.max_words(dur)}. Use Shorten to fit, or pick "
                                  + (f"{fit} seconds." if fit else "a longer length."), "script")
            chk = compliance.check(said)
            if not chk["ok"] or creator.CUSTOMER_VOICE in chk["warnings"]:
                raise ConfigError(f"Script {i + 1}: " + " ".join(chk["blocks"] or [creator.CUSTOMER_VOICE]), "script")
            for who in cast:
                for asp in aspects:
                    params = dict(base, duration=dur, aspect=asp, setting=str(d.get("setting") or "")[:120], resolution=res)
                    if typ == "ugc":
                        params.update(presenter=who, voice=voice or _default_voice(who))
                        if d.get("placement") == "holding":     # the old look: the presenter holds the product
                            params["placement"] = "holding"
                        label = f"{catalog.PRESENTER_BY_ID[who]['name']} · {asp}{hd}"
                        where = place(who)
                        if where:                               # m15: the presenter's situation (and its still)
                            params["situation"] = where
                            label = (f"{catalog.PRESENTER_BY_ID[who]['name']} · "
                                     f"{presenters.SITUATIONS[where]['label']} · {asp}{hd}")
                    else:
                        params.update(voice=who)
                        label = f"{catalog.VOICE_BY_ID[who]['name']} · {asp}{hd}"
                    if rec is not None:
                        params["preset"] = rec.id
                    jobs.append({"tool": tool, "brief": said, "params": params, "credits": credits, "usd": usd,
                                 "label": label, "hook": item, "key": f"{i}:{who}:{asp}"})
    elif kind == "image":
        n = max(1, min(4, int(d.get("versions") or 1)))
        credits, usd = catalog.price_credits(tool, batch=n), catalog.cost_usd(tool, batch=n)
        subline, cta = image_text(d)
        pre = presets.get(d.get("preset"))
        pre = pre if pre is not None and pre.tool == "ad_image" and pre.kind != "type" else None
        layered = pre if pre is not None and pre.layout and presets.text_layers_on() else None
        if pre is not None and pre.layout and layered is None:
            raise ConfigError("That preset is switched off right now. Pick another.", "type")
        layers = {}
        if layered is not None:
            try:
                layers = presets.clean_layers(layered, d.get("layers"), b)
            except presets.LayerError as e:
                raise ConfigError(str(e), "script") from None
            if not photo_ids:
                raise ConfigError(f"{p['name']} needs a photo first: this preset is built around it.", "format")
            if layered.extra.get("headline") is False:
                items, subline, cta = [layered.name], "", ""
            if "cta" not in layered.text_layers:
                cta = ""
            if "subhead" not in layered.text_layers:
                subline = ""
        if body:
            chk = compliance.check(body)
            if not chk["ok"]:
                raise ConfigError("Image concept: " + " ".join(chk["blocks"]), "script")
        for label, txt in (("Subline", subline), ("Button text", cta)):
            chk = compliance.check(txt)
            if txt and not chk["ok"]:
                raise ConfigError(f"{label}: " + " ".join(chk["blocks"]), "script")
        noun = "Review" if layered is not None and layered.layout == "review" else "Headline"
        for i, item in enumerate(items):
            item = item[:(layered.extra.get("item_max") if layered is not None else 0) or catalog.TEXT_LIMITS["headline"]]
            chk = compliance.check(item)
            if not chk["ok"]:
                raise ConfigError(f"{noun} {i + 1}: " + " ".join(chk["blocks"]), "script")
            item = item.replace('"', "'")
            text = f'Headline: "{item}"' + (f' Subline: "{subline}"' if subline else "") + (f' Button: "{cta}"' if cta else "")
            brief_txt = (body[:2000 - len(text) - 1] + " " + text).strip()
            for asp in aspects:
                params = dict(base, batch=n, aspect=asp, audience=(b.get("audience") or "")[:200], niche="",
                              headline=item, subline=subline, cta=cta)
                if pre is not None:
                    params["preset"] = pre.id         # which preset made it (acceptance per preset)
                if layered is not None:               # its words are set by textlayer.py, not by the image model
                    params.update(layout=layered.layout, layers=layers)
                    if layers.get("attest"):
                        params["review_attested_by"] = g.user["id"]
                    if layers.get("true"):                # "both columns are true" (the honest before and after)
                        params["attested_by"] = g.user["id"]
                jobs.append({"tool": tool, "brief": brief_txt[:2000], "params": params, "credits": credits, "usd": usd,
                             "label": f"{asp} · {n} version{'s' if n > 1 else ''}", "hook": item, "key": f"{i}::{asp}"})
    elif tool == "photo_to_video":
        dur = 10 if int(d.get("duration") or 5) == 10 else 5
        credits, usd = catalog.price_credits(tool, duration=dur, resolution=res), catalog.cost_usd(tool, duration=dur,
                                                                                                    resolution=res)
        for i, item in enumerate(items):
            chk = compliance.check(item)
            if not chk["ok"]:
                raise ConfigError(f"Scene {i + 1}: " + " ".join(chk["blocks"]), "script")
            jobs.append({"tool": tool, "brief": item, "params": dict(base, photo_ids=photo_ids[:1], duration=dur,
                                                                      audio=bool(d.get("audio")), resolution=res),
                         "credits": credits, "usd": usd, "label": f"{dur}s{hd}", "hook": item, "key": f"{i}::"})
    else:
        ref = conn.execute("SELECT * FROM refs WHERE id=? AND active=1", (d.get("ref") or 0,)).fetchone()
        if not ref:
            raise ConfigError("Pick a reference clip.", "format")
        secs = ref["seconds"] or 15
        credits, usd = catalog.price_credits(tool, ref_seconds=secs), catalog.cost_usd(tool, ref_seconds=secs)
        for i, item in enumerate(items):
            chk = compliance.check(item)
            if not chk["ok"]:
                raise ConfigError(f"Scene {i + 1}: " + " ".join(chk["blocks"]), "script")
            jobs.append({"tool": tool, "brief": item, "params": dict(base, ref_id=ref["id"]), "credits": credits,
                         "usd": usd, "label": ref["title"], "hook": item, "key": f"{i}::"})
    every, alljobs = len(jobs), list(jobs)
    if isinstance(d.get("lines"), list):
        want = {str(x) for x in d["lines"]}
        jobs = [j for j in jobs if j["key"] in want]
        if not jobs:
            raise ConfigError("Tick at least one ad to make.", "review")
    if len(jobs) > MAX_BATCH:
        raise ConfigError(f"That's {len(jobs)} ads; one batch makes at most {MAX_BATCH}. Untick some below, or pick "
                          "fewer hooks, presenters or shapes.", "review" if every > len(jobs) or d.get("lines") else "cast"
                          if kind == "spoken" else "format")
    return {"product": p, "type": at, "tool": tool, "every": every, "all": alljobs, "brief": b}, jobs


def image_text(d) -> tuple[str, str]:
    """The optional subline and button text the member typed for an image ad
    (the only words besides the headline and brand name allowed on it)."""
    clean = lambda k, lim: " ".join(str(d.get(k) or "").split())[:lim].replace('"', "'")  # noqa: E731
    return clean("subline", catalog.TEXT_LIMITS["subline"]), clean("cta", catalog.TEXT_LIMITS["cta"])


def _default_voice(presenter_id):
    return catalog.default_voice(presenter_id)


@bp.route("/app/create/quote", methods=["POST"])
@login_required
def quote():
    return quote_for(_json())


def quote_for(d):
    """The price of a flow config, as JSON (Create's quote; the Studio composer sends its config here too)."""
    with db.connect() as conn:
        try:
            meta, jobs = expand(conn, d)
        except ConfigError as e:
            return jsonify({"error": str(e), "step": e.step, "count": 0, "total": 0, "balance": db.balance(g.ws)})
        everything = meta["all"]
    total, bal = sum(j["credits"] for j in jobs), db.balance(g.ws)
    chosen = {j["key"] for j in jobs}
    videos = meta["tool"] in VIDEO_TOOLS
    return jsonify({"count": len(jobs), "total": total, "each": jobs[0]["credits"] if jobs else 0,
                    "every": len(everything), "videos": videos, "warn": videos and len(jobs) > WARN_VIDEOS,
                    "confirm": videos and len(jobs) > CONFIRM_VIDEOS, "max": MAX_BATCH,
                    "balance": bal, "short": max(0, total - bal), "pack": suggest_pack(total - bal),
                    "lines": [{"key": j["key"], "label": j["label"], "hook": j["hook"][:90], "credits": j["credits"],
                               "on": j["key"] in chosen} for j in everything]})


@bp.route("/app/create/check", methods=["POST"])
@login_required
def check_step():
    """Free, instant checks for the script step (no AI): words per version
    against the length, and the compliance rules on every line."""
    d = _json()
    typ = d.get("type") if d.get("type") in catalog.AD_TYPE_BY_ID else "ugc"
    kind = catalog.AD_TYPE_BY_ID[typ]["script"]
    items = [" ".join(str(x).split())[:900] for x in (d.get("items") or [])[:MAX_ITEMS]]
    body = " ".join(str(d.get("body") or "").split())[:2000]
    dur = int(d.get("duration") or 10) if str(d.get("duration") or "10").isdigit() else 10
    lim, hard = catalog.max_words(dur), catalog.hard_words(dur)
    out = []
    for i, item in enumerate(items):
        # Spoken lines are said with the body, so they're checked together (as
        # generate does). A headline or scene is checked on its own and the
        # image concept separately: a rule the concept breaks is shown on the
        # concept, never on a clean headline.
        said = (item + " " + body).strip() if kind == "spoken" else item
        chk = compliance.check(said)
        blocks = list(chk["blocks"])
        if kind == "spoken" and creator.CUSTOMER_VOICE in chk["warnings"]:
            blocks.append(creator.CUSTOMER_VOICE)
        words = len(said.split())
        over = kind == "spoken" and words > hard
        out.append({"i": i, "words": words, "over": over, "blocks": blocks, "seconds": catalog.seconds_for(words),
                    "fits": catalog.fits_duration(words) if over else None})
    bchk = compliance.check(body) if body else {"blocks": []}
    text_blocks = []
    if kind == "image":
        subline, cta = image_text(d)
        text_blocks = compliance.check(subline, cta)["blocks"] if (subline or cta) else []
    pre = presets.get(d.get("preset")) if kind == "image" else None
    if pre is not None and pre.layout and presets.text_layers_on():
        with db.connect() as conn:
            prow = _product_row(conn, d.get("product"))
        try:
            presets.clean_layers(pre, d.get("layers"), brand(prow["id"]) if prow is not None else {})
        except presets.LayerError as e:
            text_blocks = list(text_blocks) + [str(e)]
    return jsonify({"items": out, "limit": lim, "hard": hard, "body_blocks": bchk["blocks"], "text_blocks": text_blocks,
                    "ok": not any(x["over"] or x["blocks"] for x in out) and not bchk["blocks"] and not text_blocks})


def _claims_state(conn, d, p, b):
    """(lines, hash, cached result or None) of a config's claims check."""
    lines = claims_check.lines_of(d)
    src = claims_check.sources(b, claims_check.page_text_of(p))
    h = claims_check.text_hash(lines, src)
    return lines, h, claims_check.cached(conn, g.ws["id"], p["id"], h)


@bp.route("/app/create/claims", methods=["POST"])
@login_required
def claims():
    """The claims check for the script step (CLAIMS_CHECK): every factual sentence must trace back to an approved
    claim, the product's saved details or its page. One AI call, free to the member (m15; "charged" is always 0);
    the result is kept, so the same words are never checked twice, and a daily cap bounds what we spend.
    {"status": ok | flagged | skipped | off, "flags": [{line, sentence, why}]}."""
    return claims_for(_json())


def claims_for(d):
    if not claims_check.on():
        return jsonify({"status": "off", "flags": []})
    with db.connect() as conn:
        p = _product_row(conn, d.get("product"))
        if p is None:
            abort(404)
        b = brand(p["id"])
        lines, h, have = _claims_state(conn, d, p, b)
        if not lines:
            return jsonify({"status": "ok", "flags": [], "charged": 0, "balance": _bal()})
        if have is not None and have.get("status") in ("ok", "flagged"):
            return jsonify(dict(have, charged=0, balance=_bal(), cached=True))
        if not llm.anthropic_ready():
            return jsonify({"status": "skipped", "flags": [], "charged": 0, "balance": _bal()})
        usage = llm.Usage()
        if claims_check.over_cap(conn, g.ws["id"]):
            # today's safety cap is used (the check is free to members, so it is capped): the script goes through on
            # the fixed rules alone, exactly as when the model is down
            res = {"status": "skipped", "flags": [], "facts": 0, "supported": 0}
        else:
            try:
                res = claims_check.check(lines, b, claims_check.page_text_of(p), usage)
            except llm.LLMError:
                # the check couldn't run: the script goes through on the fixed rules alone, as it did before
                res = {"status": "skipped", "flags": [], "facts": 0, "supported": 0}
        # m15: free to the member. We pay what the call cost (usage.usd, kept on the row for the admin's numbers).
        claims_check.save(conn, g.ws["id"], p["id"], h, res, round(usage.usd, 6))
        conn.commit()
    return jsonify(dict(res, charged=0, balance=_bal()))


@bp.route("/app/create/generate", methods=["POST"])
@login_required
def generate():
    return generate_for(_json())


def generate_for(d):
    """Start a flow config's jobs. `d["total"]` is the total the member was shown: nothing starts unless it still
    matches the price worked out here (Create and the Studio composer both end up in this function)."""
    with db.connect() as conn:
        try:
            meta, jobs = expand(conn, d)
        except ConfigError as e:
            return jsonify({"error": str(e), "step": e.step}), 400
        total = sum(j["credits"] for j in jobs)
        if claims_check.on() and llm.anthropic_ready():
            # CLAIMS_CHECK: the words about to be made need a clean check (the page runs it at the script step)
            lines, _h, have = _claims_state(conn, d, meta["product"], meta["brief"])
            if lines and have is None:
                return jsonify({"error": "Check the claims in this script first.", "step": "script", "claims": True}), 400
            if lines and have.get("status") == "flagged":
                return jsonify({"error": "Some lines say things the product page doesn't back up. Edit or remove them.",
                                "step": "script", "claims": True, "flags": have.get("flags") or []}), 400
        hold_check(conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone())
        blocked = verify_block(conn, meta["tool"], total)
        if blocked:     # video (or past the unconfirmed allowance): the page asks for the code, then sends this again
            return verify_json(blocked)
        if meta["tool"] in VIDEO_TOOLS and len(jobs) > CONFIRM_VIDEOS and not d.get("confirm_many"):
            return jsonify({"error": f"That's {len(jobs)} videos ({total:,} credits). Tick the box to "
                                     "confirm you want all of them, or untick some.", "confirm": True}), 400
        if str(total) != str(d.get("total")):
            return jsonify({"error": f"The total changed to {total:,} credits. Check it and confirm again.",
                            "total": total}), 409
        if db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()) < total:
            return jsonify({"error": f"That needs {total:,} credits and you have {db.balance(g.ws):,}. Pick fewer or "
                                     "top up on Billing.", "billing": url_for("billing_page"),
                            "pack": suggest_pack(total - db.balance(g.ws))}), 402
        p = meta["product"]
        angle = d.get("angle") or {}
        aid = save_angle(conn, p["id"], angle, "ai" if angle.get("ai") else "user") if angle.get("title") else None
        save_script(conn, p["id"], meta["type"]["id"], d.get("items"), d.get("body"), angle.get("title"), aid)
        cfg = {k: d.get(k) for k in ("type", "angle", "items", "body", "presenters", "voices", "voice", "duration",
                                     "aspects", "platforms", "versions", "ref", "photos", "setting", "audio", "resolution",
                                     "subline", "cta", "placement", "preset", "layers", "situation")}
        cfg["made"] = [j["key"] for j in jobs]
        cfg["every"] = meta["every"]
        cfg["product"] = p["id"]
        title = (angle.get("title") or jobs[0]["hook"])[:80]
        bid = conn.execute("INSERT INTO batches (workspace_id, user_id, product_id, ad_type, title, config_json, credits, "
                           "created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (g.ws["id"], g.user["id"], p["id"], meta["type"]["id"], title, json.dumps(cfg), total,
                            db.now())).lastrowid
        conn.commit()
        queued = 0
        for j in jobs:
            params = dict(j["params"], label=j["label"], hook=j["hook"][:200], line=j["key"])
            tpl_id = params.get("preset") if params.get("preset") in catalog.TEMPLATE_BY_ID else None
            jid = queue_job(conn, j["tool"], tpl_id, j["brief"], params, j["usd"], j["credits"], quiet=True,
                            product_id=p["id"], batch_id=bid)
            if not jid:
                break
            queued += 1
        conn.execute("UPDATE batches SET credits=? WHERE id=?", (sum(j["credits"] for j in jobs[:queued]), bid))
        if d.get("from_ad"):
            first = conn.execute("SELECT id FROM jobs WHERE batch_id=? ORDER BY id LIMIT 1", (bid,)).fetchone()
            conn.execute("UPDATE campaign_ads SET status='queued', job_id=? WHERE id=? AND workspace_id=? AND status='proposed'",
                         (first["id"] if first else None, d.get("from_ad"), g.ws["id"]))
        conn.commit()
    return jsonify({"ok": True, "batch": bid, "queued": queued, "url": url_for("create.batch", bid=bid)})


@bp.route("/app/create/batch/<int:bid>")
@login_required
def batch(bid):
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id=? AND workspace_id=?", (bid, g.ws["id"])).fetchone()
        if not b:
            abort(404)
        jobs = conn.execute("SELECT * FROM jobs WHERE batch_id=? AND workspace_id=? AND deleted_at IS NULL ORDER BY id",
                            (bid, g.ws["id"])).fetchall()
        outs = {}
        if jobs:
            ids = [j["id"] for j in jobs]
            for o in conn.execute(f"SELECT * FROM outputs WHERE job_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
                outs.setdefault(o["job_id"], []).append(o)
        p = products.get_product(conn, g.ws["id"], b["product_id"]) if b["product_id"] else None
        first = conn.execute("SELECT COUNT(*) FROM batches WHERE workspace_id=? AND id<?", (g.ws["id"], bid)).fetchone()[0] == 0
        nphotos = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (b["product_id"] or 0,)).fetchone()[0]
        import jobs_routes
        verdicts = jobs_routes.verdicts(conn, [j["id"] for j in jobs])
        prices = {j["id"]: jobs_routes.price_now(conn, j)[0] for j in jobs if j["status"] == "failed"}
        eta = jobs_routes.eta_minutes(conn)
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM products WHERE workspace_id=?",
                                                           (g.ws["id"],))}
    previews = [j for j in jobs if json.loads(j["params_json"] or "{}").get("preview")]
    full_price = catalog.price_credits("ugc_video", duration=json.loads(previews[0]["params_json"]).get("duration") or 8) \
        if previews else 0
    return render_template("batch.html", b=b, jobs=jobs, outs=outs, p=p, cfg=json.loads(b["config_json"] or "{}"),
                           at=catalog.AD_TYPE_BY_ID.get(b["ad_type"]) or {}, templates=catalog.TEMPLATE_BY_ID,
                           first_batch=first, nphotos=nphotos, is_preview=bool(previews), full_price=full_price,
                           verdicts=verdicts, prices=prices, eta=eta, names=names)


# ---------------------------------------------------------------- the library's own assets (m15, site admins only)

def _own_workspace():
    """The library is made in the admin's OWN workspace: not while they are looking at someone else's."""
    if g.get("viewing"):
        abort(403, "Go back to your own workspace first: library assets are made there.")


def _library_form():
    f = request.form
    ids = [x for x in (f.get("ids") or "").replace(",", " ").split() if x]
    options = {k: f.get(k) for k in ("quality", "resolution", "demo", "presenter", "situation", "voice") if f.get(k)}
    for k in ("anchor_only", "only_missing"):
        if f.get(k):
            options[k] = True
    return (f.get("kind") or ""), ids, options


@bp.route("/admin/library", methods=["GET", "POST"])
@admin_required
def library_admin():
    """Make, review and export the library's assets: presenter stills and voice samples, the demo products' packshots
    and preset examples. Quote first; Generate needs the quoted total (library.py)."""
    import hf
    import library
    _own_workspace()
    quote = error = None
    kind, ids, options = _library_form() if request.method == "POST" else (request.args.get("kind") or "", [], {})
    vendor_presets = None
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
        if request.method == "POST":
            do = request.form.get("do")
            try:
                if do == "vendor_presets":
                    vendor_presets = hf.vendor_presets()          # a free read of the vendor's own preset catalog
                elif do == "generate":
                    res = library.generate(conn, ws, kind, ids, options, request.form.get("total"))
                    flash(f"Started {res['queued']} job(s) for {res['credits_charged']:,} credits.", "ok")
                    return redirect(url_for("create.library_admin", kind=kind) + "#made")
                else:
                    quote = library.quote(conn, ws, kind, ids, options)
            except library.LibraryError as e:
                error = str(e)
            except hf.GenError as e:
                error = f"The vendor refused: {e}"
        rows = library.assets(conn, g.ws["id"], request.args.get("show") or None)
        info = library.overview(conn, g.ws["id"])
    return render_template("admin_library.html", rows=rows, info=info, quote=quote, error=error, kind=kind,
                           ids=" ".join(ids), options=options, kinds=library.KINDS, demo=library.DEMO,
                           roster=presenters.all_presenters(), situations=presenters.SITUATIONS,
                           still_presets=[x for x in presets.LIBRARY if x.kind != "type"],
                           runnable=presets.runnable, example_url=presets.example_url,
                           example_video_url=presets.example_video_url, vendor_presets=vendor_presets,
                           stills_on=presenters.stills_on(), voice_ref_on=presenters.voice_ref_on(),
                           still_file=presenters.still_path,
                           credits_left=db.balance(ws), still_model=catalog.model_for("library_still"),
                           still_usd=catalog.LIBRARY_STILL_USD)


@bp.route("/admin/library/export.zip")
@admin_required
def library_export():
    """The finished assets (the approved one of each, else the newest), named by their tags, for
    tools/import_library_assets.py."""
    import library
    _own_workspace()
    with db.connect() as conn:
        data = library.export_zip(conn, g.ws["id"], request.args.get("kind") or None)
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=campaignify-library.zip",
                             "Cache-Control": "private, no-store"})

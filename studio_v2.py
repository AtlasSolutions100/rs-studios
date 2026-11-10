"""Studio: one composer over the same pipeline as Create (M14, behind ui_v2.on()).

The page (web/v2/studio.html, static/studio_v2.js) is a canvas of the workspace's latest ads with a composer
at the bottom: pick what to make, the product and (for a presenter video) who says it, type the words, press
send. Send never starts anything by itself:

    send     -> POST /app/studio/quote     the price of exactly what is in the composer (create_routes.quote_for)
    confirm  -> POST /app/studio/generate  the same composer contents plus `total`, the price that was shown

`config_for` turns the composer's small payload into the flow config Create has always used, and both routes
hand that config to create_routes, so the compliance rules, the claims check, the email-confirm step, the
batch limits and the "the total changed" refusal are the ones Create has. A generate call without the total
the member saw is refused there (409), exactly as on Create.

The brand screen (/app/studio/brand) is one input, "Drop your website": it runs the first run's free importer
(app.start_import, no AI read, nothing charged), then shows the brand's Look (colours, font, logo: the kit a
text-layer preset is drawn with), saved through the existing brand form (product_routes.brand_edit).
"""
import json

from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for

import catalog
import claims_check
import create_routes
import db
import free_fix
import llm
import onboarding
import presenter_stills
import presets
import products
import ui_v2
from core import login_required

bp = Blueprint("studio_v2", __name__)

# What the composer can make. `type` is the Create ad type it runs as; `noun` is what the text box holds.
MODES = [
    {"id": "presenter", "type": "ugc", "name": "Presenter video", "noun": "script",
     "placeholder": "Write what the presenter says to camera",
     "line": "A presenter says your script to camera with your product beside them."},
    {"id": "voiceover", "type": "voiceover", "name": "Voiceover", "noun": "script",
     "placeholder": "Write what the narrator says over your product",
     "line": "Your product on screen while a narrator reads the script."},
    {"id": "product_video", "type": "product_video", "name": "Product video", "noun": "scene",
     "placeholder": "Describe the shot: how the product moves, where it sits, the light",
     "line": "A 5 or 10 second clip that brings one product photo to life."},
    {"id": "image", "type": "image", "name": "Image ad", "noun": "headline",
     "placeholder": "Write the headline for your image ad",
     "line": "Finished ad images with your headline."},
]
MODE_BY_ID = {m["id"]: m for m in MODES}
MODE_OF_TYPE = {m["type"]: m["id"] for m in MODES}
CANVAS_JOBS = 24
MAX_COUNT = 4          # image versions per headline (create_routes.expand allows 1 to 4)


def _clean(v, lim):
    return " ".join(str(v or "").split())[:lim]


def available_modes() -> list[dict]:
    """The modes on offer right now (talking videos can be switched off: catalog.capabilities)."""
    have = {a["id"] for a in catalog.ad_types()}
    return [m for m in MODES if m["type"] in have]


def config_for(d: dict) -> dict:
    """The Create flow config for what is in the composer. Unknown or missing values fall back to the same
    defaults Create uses; create_routes.expand validates the result."""
    mode = MODE_BY_ID.get(d.get("mode")) or MODE_BY_ID["image"]
    typ = mode["type"]
    text = str(d.get("text") or "").strip()
    cfg = {"type": typ, "product": d.get("product"), "items": [text] if text else [], "body": "",
           "resolution": d.get("resolution") or catalog.DEFAULT_RESOLUTION}
    aspect = str(d.get("aspect") or "")
    if typ in ("ugc", "voiceover"):
        durations = catalog.TOOLS["ugc_video"]["durations"]
        dur = d.get("duration")
        if not str(dur or "").isdigit() or int(dur) not in durations:
            # "Auto": the shortest length the script fits as typed
            dur = catalog.fits_duration(len(text.split())) or durations[-1]
        cfg.update(duration=int(dur), aspects=[aspect] if aspect else ["9:16"])
        if typ == "ugc":
            cfg.update(presenters=[x for x in (d.get("presenters") or []) if isinstance(x, str)][:8],
                       voice=d.get("voice") or "", setting=_clean(d.get("setting"), 120))
        else:
            cfg.update(voices=[d.get("voice")] if d.get("voice") else [])
    elif typ == "product_video":
        cfg.update(duration=10 if str(d.get("duration")) == "10" else 5)
    else:
        try:
            n = int(d.get("count") or 1)
        except (TypeError, ValueError):
            n = 1
        pre = presets.get(d.get("preset"))
        pre = pre if pre is not None and pre.tool == "ad_image" and pre.kind != "type" else None
        cfg.update(versions=max(1, min(MAX_COUNT, n)), body=_clean(d.get("scene"), 2000),
                   aspects=[aspect] if aspect else [(pre.aspect if pre is not None else "") or "4:5"],
                   subline=d.get("subline") or "", cta=d.get("cta") or "")
        if pre is not None:
            cfg.update(preset=pre.id, layers=d.get("layers") if isinstance(d.get("layers"), dict) else {})
            if pre.layout and pre.extra.get("headline") is False and not cfg["items"]:
                cfg["items"] = [pre.name]          # the marketplace image has no words: its one line is a label
    if d.get("confirm_many"):
        cfg["confirm_many"] = True
    if "total" in d:
        cfg["total"] = d.get("total")
    return cfg


def _payload():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _off():
    """Studio's JSON routes while the new look is off for this member."""
    return jsonify({"error": "Studio isn't switched on for this account."}), 404


@bp.route("/app/studio/quote", methods=["POST"])
@login_required
def quote():
    if not ui_v2.on():
        return _off()
    return create_routes.quote_for(config_for(_payload()))


@bp.route("/app/studio/claims", methods=["POST"])
@login_required
def claims():
    if not ui_v2.on():
        return _off()
    return create_routes.claims_for(config_for(_payload()))


@bp.route("/app/studio/generate", methods=["POST"])
@login_required
def generate():
    """Start what is in the composer. The payload must carry `total`, the price the member confirmed: without
    it, or when the price has changed since, nothing starts (409 from create_routes.generate_for)."""
    if not ui_v2.on():
        return _off()
    d = _payload()
    if d.get("total") in (None, ""):
        return jsonify({"error": "See the price first, then confirm it.", "needs_quote": True}), 409
    out = create_routes.generate_for(config_for(d))
    resp, status = out if isinstance(out, tuple) else (out, 200)
    if status == 200:
        data = resp.get_json() or {}
        if data.get("ok"):
            data["url"] = url_for("studio_v2.page", made=data.get("batch"))
            return jsonify(data)
    return out


# ---------------------------------------------------------------- the page

def _prefill_from_batch(conn, bid) -> dict:
    """"More like this": an earlier batch's config, as composer contents."""
    b = conn.execute("SELECT * FROM batches WHERE id=? AND workspace_id=?", (bid, g.ws["id"])).fetchone()
    if not b:
        return {}
    c = json.loads(b["config_json"] or "{}")
    mode = MODE_OF_TYPE.get(c.get("type"))
    if not mode:
        return {}
    text = " ".join(x for x in ((c.get("items") or [""])[0], c.get("body") if mode in ("presenter", "voiceover") else "")
                    if x).strip()
    out = {"mode": mode, "text": text, "product": b["product_id"], "aspect": (c.get("aspects") or [""])[0] or "",
           "resolution": c.get("resolution") or "", "from_batch": b["id"]}
    if mode == "presenter":
        out.update(presenters=c.get("presenters") or [], duration=c.get("duration"), setting=c.get("setting") or "")
    elif mode == "voiceover":
        out.update(voice=(c.get("voices") or [""])[0], duration=c.get("duration"))
    elif mode == "product_video":
        out.update(duration=c.get("duration"))
    else:
        out.update(count=c.get("versions") or 1, scene=c.get("body") or "", preset=c.get("preset") or "",
                   layers=c.get("layers") or {}, subline=c.get("subline") or "", cta=c.get("cta") or "")
    return out


def gallery(product=None) -> list[dict]:
    """Everything "More" offers: the ad types, then the presets that fit this product. `opens` says what picking
    it does: "mode" switches the composer, "preset" pins it on an image ad, "scene" fills a product video's
    scene, "advanced" opens Create (a reference clip can't be picked in the composer)."""
    out = []
    prices = catalog.from_prices()
    for a in catalog.ad_types():
        out.append({"id": "type:" + a["id"], "name": a["name"], "what_for": a["line"], "group": "type",
                    "badge": "Ad type", "from": prices.get(a["tool"], 0),
                    "preview": url_for("static", filename=a["preview"] + ".jpg"),
                    "opens": "mode" if a["id"] in MODE_OF_TYPE else "advanced", "mode": MODE_OF_TYPE.get(a["id"], ""),
                    "href": url_for("create.index", type=a["id"]) if a["id"] not in MODE_OF_TYPE else ""})
    b = create_routes.brand(product["id"]) if product is not None else {}
    for p in presets.library_for(product):
        c = presets.card(p, url_for)
        item = {"id": p.id, "name": p.name, "what_for": p.what_for or c["line"], "from": c["from"],
                "preview": c["preview"], "aspect": p.aspect}
        if p.tool == "ad_image":
            fill = catalog.template_prefill(p.as_template(), b) if b else {}
            item.update(group="image", badge="Text layer" if p.layout else "Image preset", opens="preset",
                        mode="image", layout=p.layout, headline=(fill.get("items") or [""])[0] if not p.layout else "",
                        scene=fill.get("body") or "", spec=presets.layout_spec(p) if p.layout else None)
        elif p.tool == "photo_to_video":
            fill = catalog.template_prefill(p.as_template(), b) if b else {}
            item.update(group="video", badge="Video preset", opens="scene", mode="product_video",
                        scene=(fill.get("items") or [""])[0], duration=fill.get("duration") or 5)
        else:
            item.update(group="video", badge="Video preset", opens="advanced", mode="",
                        href=url_for("create.index", template=p.id))
        out.append(item)
    return out


def first_mode(goals, balance: int, modes) -> tuple[str, str]:
    """The composer's first mode for a new account: the first thing they said they want to make that their
    credits cover, else an image ad. Returns (mode id, a note when their pick costs more than they have)."""
    have = {m["id"] for m in modes}
    prices = catalog.from_prices()
    cost = {"presenter": prices["ugc_video"], "voiceover": prices["ugc_video"],
            "product_video": prices["photo_to_video"], "image": prices["ad_image"]}
    wanted = [onboarding.GOAL_MODE[x] for x in goals or [] if onboarding.GOAL_MODE.get(x) in have]
    pick = next((m for m in wanted if cost[m] <= balance), "image" if "image" in have else modes[0]["id"])
    note = ""
    if wanted and wanted[0] != pick and cost[wanted[0]] > balance:
        note = (f"{MODE_BY_ID[wanted[0]]['name']}s start at {cost[wanted[0]]:,} credits. "
                f"Your credits cover {MODE_BY_ID[pick]['name'].lower()}s, so that is where this starts.")
    return pick, note


@bp.route("/app/studio")
@login_required
def page():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import jobs_routes
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        jobs = conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND deleted_at IS NULL ORDER BY id DESC LIMIT ?",
                            (g.ws["id"], CANVAS_JOBS)).fetchall()
        ids = [j["id"] for j in jobs]
        outs = {}
        if ids:
            for o in conn.execute(f"SELECT * FROM outputs WHERE job_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
                outs.setdefault(o["job_id"], []).append(o)
        verdicts = jobs_routes.verdicts(conn, ids)
        prices = {j["id"]: jobs_routes.price_now(conn, j)[0] for j in jobs if j["status"] == "failed"}
        eta = jobs_routes.eta_minutes(conn)
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM products WHERE workspace_id=?",
                                                           (g.ws["id"],))}
        recents = conn.execute("SELECT b.id, b.title, b.ad_type, b.created_at, "
                               "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id AND j.status IN ('pending','running')) busy "
                               "FROM batches b WHERE b.workspace_id=? ORDER BY b.id DESC LIMIT 8", (g.ws["id"],)).fetchall()
        tree = products.tree(conn, ws)
        ever = conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (g.ws["id"],)).fetchone() is not None
        prefill = _prefill_from_batch(conn, request.args.get("batch", type=int)) if request.args.get("batch") else {}
        active = products.get_product(conn, g.ws["id"], prefill.get("product") or g.ws["product_id"]) \
            if (prefill.get("product") or g.ws["product_id"]) else None
        plist = []
        for row in tree:
            if row["locked"]:
                continue
            for p in row["products"]:
                plist.append({"id": p["id"], "name": p["name"], "brand": row["brand"]["brand_name"] or "",
                              "photo": url_for("photo", pid=p["primary_photo_id"]) if p["primary_photo_id"] else "",
                              "price": p["price"] or "", "offer": (p["offer"] if "offer" in p.keys() else "") or ""})
        if active is not None and active["id"] not in {p["id"] for p in plist}:
            active = None
        gal = gallery(active)
    bal = db.balance(ws)
    modes = available_modes()
    answers = onboarding.answers(g.user)
    mode, note = (prefill.get("mode"), "") if prefill.get("mode") else first_mode(answers.get("goals"), bal, modes)
    want = request.args.get("mode")
    if want in {m["id"] for m in modes}:
        mode, note = want, ""
    first_run = not ever
    if not first_run:
        note = ""
    img = catalog.price_credits("ad_image", batch=1)
    free_images = bal // img if first_run and img else 0
    if first_run and mode == "image" and not prefill:
        prefill = {"count": max(1, min(MAX_COUNT, free_images or 1, catalog.STARTER_IMAGE_ADS))}
    pre_req = presets.get(request.args.get("preset") or "")
    if pre_req is not None and not prefill:
        prefill = {"pick": pre_req.id}
    talk = catalog.TOOLS["ugc_video"]
    pres = [{"id": p["id"], "name": p["name"], "vibe": p["vibe"], "look": p["look"], "setting": p["setting"],
             "hue": p["hue"], "photo": presenter_stills.photo_url(p["id"], url_for) or "",
             "voice": catalog.VOICE_BY_ID[catalog.default_voice(p["id"])]["name"],
             "who": catalog.VOICE_BY_ID[catalog.default_voice(p["id"])].get("who") or ""} for p in catalog.PRESENTERS]
    boot = {
        "csrf": _csrf(), "modes": modes, "mode": mode, "note": note, "balance": bal, "first_run": first_run,
        "free_images": free_images, "products": plist, "product": active["id"] if active is not None else None,
        "presenters": pres, "max_presenters": create_routes.MAX_PRESENTERS,
        "voices": [{"id": v["id"], "name": v["name"], "who": v.get("who") or ""} for v in catalog.VOICES],
        "durations": {"talk": talk["durations"], "video": catalog.TOOLS["photo_to_video"]["durations"]},
        "aspects": {"talk": talk.get("aspects") or [], "image": catalog.TOOLS["ad_image"]["aspects"]},
        "resolutions": {"talk": list(catalog.resolutions("ugc_video")), "video": list(catalog.resolutions("photo_to_video"))},
        "max_count": MAX_COUNT, "gallery": gal, "prefill": prefill,
        "claims_check": claims_check.on() and llm.anthropic_ready(),
        "claims_credits": claims_check.estimate() if claims_check.on() and llm.anthropic_ready() else 0,
        "words": {str(d): catalog.hard_words(d) for d in talk["durations"]},
        "website": answers.get("website") or "",
        "urls": {"quote": url_for("studio_v2.quote"), "generate": url_for("studio_v2.generate"),
                 "claims": url_for("studio_v2.claims"), "import": url_for("start_import"),
                 "switch": url_for("products.switch"), "new_product": url_for("products.new"),
                 "billing": url_for("billing_page"), "studio": url_for("studio_v2.page"),
                 "advanced": url_for("create.index")},
    }
    return render_template("v2/studio.html", boot=boot, jobs=jobs, outs=outs, verdicts=verdicts, prices=prices, eta=eta,
                           names=names, recents=recents, templates=catalog.TEMPLATE_BY_ID, tools=catalog.DISPLAY_TOOLS,
                           made=request.args.get("made", type=int), free_fix_on=free_fix.on(),
                           ad_type_names=catalog.AD_TYPE_BY_ID, first_run=first_run, modes=modes)


def _csrf():
    from core import csrf_token
    return csrf_token()


@bp.route("/app/studio/brand")
@login_required
def brand():
    """"Drop your website": the free importer, then the brand's Look."""
    if not ui_v2.on():
        return redirect(url_for("brand_kit"))
    import textlayer
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        b = products.get_brand(conn, g.ws["id"], ws["brand_id"]) if ws["brand_id"] else None
        rows = products.products(conn, g.ws["id"], b["id"]) if b is not None else []
        locked = b is not None and b["id"] not in products.unlocked_brand_ids(conn, ws)
    return render_template("v2/brand.html", b=b, rows=rows, locked=locked,
                           website=onboarding.answers(g.user).get("website") or (b["website"] if b is not None else "") or "",
                           kit_fonts=[(k, v["name"]) for k, v in textlayer.FONTS.items()],
                           kit_default=textlayer.DEFAULT_KIT, imported=request.args.get("imported", type=int))

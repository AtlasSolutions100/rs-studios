"""Create: product -> ad type -> angle -> script -> presenter and voice ->
format -> quote -> generate -> review. The heart of the app.

The page is one guided flow (web/create.html) that talks to the JSON
endpoints here. Nothing is generated until /app/create/generate, which
recomputes the price from the same config and refuses if the total the member
saw has changed. Angle and script writing are small AI calls charged at cost.
"""
import json

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, url_for

import catalog
import compliance
import creator
import db
import llm
import platforms
import products
from core import brand, can_afford, charge_ai, login_required, queue_job

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


def usd(credits) -> str:
    """Credits as dollars (1 credit = $0.01)."""
    cents = int(credits or 0)
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


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
                         ([s["hook"]] if s["hook"] else [])} for s in scripts]}


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
                prefill = dict(json.loads(b["config_json"] or "{}"), step="script")
                pid = b["product_id"] or pid
                if request.args.get("rest") and prefill.get("made"):
                    # "Make the rest": the same config, at review, with the lines already made unticked.
                    prefill.update(step="review", skip=prefill["made"], rest_of=b["id"])
                prefill.pop("made", None)
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
              "ad_image": {n: catalog.price_credits("ad_image", batch=n) for n in (1, 2, 3, 4)},
              "motion_copy": {r["id"]: catalog.price_credits("motion_copy", ref_seconds=r["seconds"] or 15) for r in refs}}
    boot = {"product": st, "prefill": prefill, "types": catalog.ad_types(), "caps": catalog.capabilities(),
            "presenters": catalog.PRESENTERS, "voices": catalog.VOICES, "prices": prices,
            "refs": [{"id": r["id"], "title": r["title"], "seconds": r["seconds"], "url": url_for("ref_video", rid=r["id"])}
                     for r in refs],
            "durations": {"ugc_video": catalog.TOOLS["ugc_video"]["durations"], "photo_to_video": [5, 10]},
            "aspects": {"ugc_video": catalog.TOOLS["ugc_video"]["aspects"], "ad_image": catalog.TOOLS["ad_image"]["aspects"]},
            "platforms": {k: platforms.PLATFORMS[k]["short"] for k in PLATFORM_ASPECTS if k in platforms.PLATFORMS},
            "platform_aspects": PLATFORM_ASPECTS, "wps": catalog.WORDS_PER_SECOND,
            "ai": llm.anthropic_ready(), "quote_angles": creator.estimate_angles(), "quote_script": creator.estimate_script(),
            "max_batch": MAX_BATCH, "max_items": MAX_ITEMS, "max_presenters": MAX_PRESENTERS,
            "warn_videos": WARN_VIDEOS, "confirm_videos": CONFIRM_VIDEOS, "balance": db.balance(g.ws),
            "urls": {"angles": url_for("create.angles"), "script": url_for("create.script"), "save": url_for("create.save"),
                     "quote": url_for("create.quote"), "generate": url_for("create.generate"),
                     "check": url_for("create.check_step"), "billing": url_for("billing_page"),
                     "state": url_for("create.product_state"), "new_product": url_for("products.new"),
                     "edit_product": url_for("products.edit", pid=0)}}
    return render_template("create.html", boot=boot, tree=tree)


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


def _ai_ready(quote):
    if not llm.anthropic_ready():
        return jsonify({"error": "AI writing isn't switched on yet. Write your own below; nothing was charged."}), 503
    if not can_afford(quote):
        return jsonify({"error": f"That needs about {quote} credits and you have {db.balance(g.ws):,}."}), 402
    return None


@bp.route("/app/create/angles", methods=["POST"])
@login_required
def angles():
    d = _json()
    with db.connect() as conn:
        p = _product_row(conn, d.get("product"))
        if p is None:
            abort(404)
        busy = _ai_ready(creator.estimate_angles())
        if busy:
            return busy
        usage = llm.Usage()
        try:
            out = creator.angles(brand(p["id"]), d.get("type") or "ugc", 5, [str(x)[:80] for x in d.get("avoid") or []],
                                 usage)
            err = None
        except llm.LLMError as e:
            out, err = [], f"The writer didn't finish ({e}). Try again."
        taken = charge_ai(conn, usage.credits, "AI angle ideas") if usage.calls else 0
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
    duration = int(d.get("duration") or 10)
    with db.connect() as conn:
        p = _product_row(conn, d.get("product"))
        if p is None:
            abort(404)
        busy = _ai_ready(creator.estimate_script())
        if busy:
            return busy
        usage = llm.Usage()
        try:
            out, err = creator.script(brand(p["id"]), typ, angle, duration, max(1, min(MAX_ITEMS, int(d.get("n") or 3))),
                                      usage), None
        except llm.LLMError as e:
            out, err = {"items": [], "body": "", "on_screen": []}, f"The writer didn't finish ({e}). Try again."
        taken = charge_ai(conn, usage.credits, "AI script") if usage.calls else 0
        conn.commit()
    return jsonify(dict(out, charged=taken, balance=_bal(), **({"error": err} if err else {})))


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
    aspects_ok = catalog.TOOLS[tool].get("aspects") or []
    aspects = list(dict.fromkeys(a for a in d.get("aspects") or [] if a in aspects_ok))[:MAX_ASPECTS] or \
        (aspects_ok[:1] if aspects_ok else [None])
    base = {"photo_ids": photo_ids, "ad_type": typ, "product": p["name"]}
    if kind == "spoken":
        dur = int(d.get("duration") or 10)
        if dur not in catalog.TOOLS[tool]["durations"]:
            raise ConfigError("Pick a length.", "script")
        limit = int(catalog.max_words(dur) * 1.2)
        if typ == "ugc":
            cast = list(dict.fromkeys(x for x in d.get("presenters") or [] if x in catalog.PRESENTER_BY_ID))
            if not cast:
                raise ConfigError("Pick at least one presenter.", "cast")
        else:
            cast = list(dict.fromkeys(x for x in d.get("voices") or [] if x in catalog.VOICE_BY_ID)) or ["narrator_deep"]
        if len(cast) > MAX_PRESENTERS:
            raise ConfigError(f"Pick at most {MAX_PRESENTERS} {'presenters' if typ == 'ugc' else 'voices'} per batch.", "cast")
        voice = d.get("voice") if d.get("voice") in catalog.VOICE_BY_ID else ""
        credits, usd = catalog.price_credits(tool, duration=dur), catalog.cost_usd(tool, duration=dur)
        for i, item in enumerate(items):
            said = (item + " " + body).strip()
            if len(said.split()) > limit:
                raise ConfigError(f"Script {i + 1} is {len(said.split())} words; a {dur}-second video fits about "
                                  f"{catalog.max_words(dur)}. Shorten it or pick a longer length.", "script")
            chk = compliance.check(said)
            if not chk["ok"] or creator.CUSTOMER_VOICE in chk["warnings"]:
                raise ConfigError(f"Script {i + 1}: " + " ".join(chk["blocks"] or [creator.CUSTOMER_VOICE]), "script")
            for who in cast:
                for asp in aspects:
                    params = dict(base, duration=dur, aspect=asp, setting=str(d.get("setting") or "")[:120])
                    if typ == "ugc":
                        params.update(presenter=who, voice=voice or _default_voice(who))
                        label = f"{catalog.PRESENTER_BY_ID[who]['name']} · {asp}"
                    else:
                        params.update(voice=who)
                        label = f"{catalog.VOICE_BY_ID[who]['name']} · {asp}"
                    jobs.append({"tool": tool, "brief": said, "params": params, "credits": credits, "usd": usd,
                                 "label": label, "hook": item, "key": f"{i}:{who}:{asp}"})
    elif kind == "image":
        n = max(1, min(4, int(d.get("versions") or 1)))
        credits, usd = catalog.price_credits(tool, batch=n), catalog.cost_usd(tool, batch=n)
        for i, item in enumerate(items):
            brief_txt = (body + (f' Headline: "{item}"' if body else f'Headline: "{item}"')).strip()
            chk = compliance.check(brief_txt)
            if not chk["ok"]:
                raise ConfigError(f"Headline {i + 1}: " + " ".join(chk["blocks"]), "script")
            for asp in aspects:
                params = dict(base, batch=n, aspect=asp, audience=(b.get("audience") or "")[:200], niche="")
                jobs.append({"tool": tool, "brief": brief_txt[:2000], "params": params, "credits": credits, "usd": usd,
                             "label": f"{asp} · {n} version{'s' if n > 1 else ''}", "hook": item, "key": f"{i}::{asp}"})
    elif tool == "photo_to_video":
        dur = 10 if int(d.get("duration") or 5) == 10 else 5
        credits, usd = catalog.price_credits(tool, duration=dur), catalog.cost_usd(tool, duration=dur)
        for i, item in enumerate(items):
            chk = compliance.check(item)
            if not chk["ok"]:
                raise ConfigError(f"Scene {i + 1}: " + " ".join(chk["blocks"]), "script")
            jobs.append({"tool": tool, "brief": item, "params": dict(base, photo_ids=photo_ids[:1], duration=dur,
                                                                      audio=bool(d.get("audio"))),
                         "credits": credits, "usd": usd, "label": f"{dur}s", "hook": item, "key": f"{i}::"})
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
    return {"product": p, "type": at, "tool": tool, "every": every, "all": alljobs}, jobs


def _default_voice(presenter_id):
    pr = catalog.PRESENTER_BY_ID.get(presenter_id) or {}
    return {"maya": "warm_f", "priya": "warm_f", "elena": "warm_f", "sam": "upbeat_f", "lena": "upbeat_f",
            "jordan": "chatty_m", "theo": "chatty_m", "marcus": "calm_m"}.get(pr.get("id"), "warm_f")


@bp.route("/app/create/quote", methods=["POST"])
@login_required
def quote():
    d = _json()
    with db.connect() as conn:
        try:
            meta, jobs = expand(conn, d)
        except ConfigError as e:
            return jsonify({"error": str(e), "step": e.step, "count": 0, "total": 0, "balance": db.balance(g.ws)})
        everything = meta["all"]
    total, bal = sum(j["credits"] for j in jobs), db.balance(g.ws)
    chosen = {j["key"] for j in jobs}
    videos = meta["tool"] in VIDEO_TOOLS
    return jsonify({"count": len(jobs), "total": total, "usd": usd(total), "each": jobs[0]["credits"] if jobs else 0,
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
    lim = catalog.max_words(dur)
    out = []
    for i, item in enumerate(items):
        said = (item + " " + body).strip() if kind == "spoken" else item
        chk = compliance.check(said if kind != "image" else (item + " " + body))
        blocks = list(chk["blocks"])
        if kind == "spoken" and creator.CUSTOMER_VOICE in chk["warnings"]:
            blocks.append(creator.CUSTOMER_VOICE)
        words = len(said.split())
        out.append({"i": i, "words": words, "over": kind == "spoken" and words > int(lim * 1.2), "blocks": blocks})
    bchk = compliance.check(body) if body else {"blocks": []}
    return jsonify({"items": out, "limit": lim, "body_blocks": bchk["blocks"],
                    "ok": not any(x["over"] or x["blocks"] for x in out) and not bchk["blocks"]})


@bp.route("/app/create/generate", methods=["POST"])
@login_required
def generate():
    d = _json()
    with db.connect() as conn:
        try:
            meta, jobs = expand(conn, d)
        except ConfigError as e:
            return jsonify({"error": str(e), "step": e.step}), 400
        total = sum(j["credits"] for j in jobs)
        if meta["tool"] in VIDEO_TOOLS and len(jobs) > CONFIRM_VIDEOS and not d.get("confirm_many"):
            return jsonify({"error": f"That's {len(jobs)} videos ({total:,} credits, {usd(total)}). Tick the box to "
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
                                     "aspects", "platforms", "versions", "ref", "photos", "setting", "audio")}
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
            jid = queue_job(conn, j["tool"], None, j["brief"], params, j["usd"], j["credits"], quiet=True,
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
        jobs = conn.execute("SELECT * FROM jobs WHERE batch_id=? AND workspace_id=? ORDER BY id", (bid, g.ws["id"])).fetchall()
        outs = {}
        if jobs:
            ids = [j["id"] for j in jobs]
            for o in conn.execute(f"SELECT * FROM outputs WHERE job_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
                outs.setdefault(o["job_id"], []).append(o)
        p = products.get_product(conn, g.ws["id"], b["product_id"]) if b["product_id"] else None
        first = conn.execute("SELECT COUNT(*) FROM batches WHERE workspace_id=? AND id<?", (g.ws["id"], bid)).fetchone()[0] == 0
        nphotos = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (b["product_id"] or 0,)).fetchone()[0]
    previews = [j for j in jobs if json.loads(j["params_json"] or "{}").get("preview")]
    full_price = catalog.price_credits("ugc_video", duration=json.loads(previews[0]["params_json"]).get("duration") or 8) \
        if previews else 0
    return render_template("batch.html", b=b, jobs=jobs, outs=outs, p=p, cfg=json.loads(b["config_json"] or "{}"),
                           at=catalog.AD_TYPE_BY_ID.get(b["ad_type"]) or {}, templates=catalog.TEMPLATE_BY_ID,
                           first_batch=first, nphotos=nphotos, is_preview=bool(previews), full_price=full_price)

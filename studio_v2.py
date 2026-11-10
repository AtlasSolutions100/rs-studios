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

M15 adds, on the same two routes and with no new paid path:

    several ads in one go    a request that stands for more than one Create config (`parts_for`): the first run's two
                             free image ads as two DIFFERENT templates (catalog.first_run_pair, the classic first
                             run's own choice), and the Workflow tab's packs (PACKS). Each part is priced by
                             create_routes.quote_for and started by create_routes.generate_for with its own total, so
                             every rule Create has still applies to each one; the member confirms the sum once.
    the presenter picker     `roster()` reads presenters.roster() when the module exists and falls back to today's
                             eight; the chosen presenter and situation ride along as `presenters` and `situation`.
    Workflow                 /app/studio/workflow: a box that hands a description to the campaign planner (its own
                             form and price note, campaign_routes.index) and a shelf of packs built from presets.
"""
import json
import logging
import re

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

import catalog
import claims_check
import compliance
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
log = logging.getLogger("campaignify.studio")

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
            # the picker's situation (presenters.roster()): an id Create may know; one it doesn't know changes nothing
            sit = d.get("situation")
            if isinstance(sit, str) and sit.strip():
                cfg["situation"] = _clean(sit, 60)
            sits = d.get("situations")
            if isinstance(sits, dict):
                cfg["situations"] = {str(k)[:60]: _clean(v, 60) for k, v in list(sits.items())[:8] if isinstance(v, str)}
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


# ---------------------------------------------------------------- several ads from one request (M15)

class PackError(ValueError):
    """A pack or pair that can't be made as asked; the message is for the member."""


# Workflow's packs: bundles of existing presets. A part is a preset by id, {"scene": n} (the n-th image template that
# suits the product, the first run's own order: catalog.first_run_category) or {"voiceover": True}. `aspect` overrides
# the preset's own shape. Nothing here has a price of its own: every part is quoted and made by Create.
PACKS = [
    {"id": "launch", "name": "Launch pack",
     "line": "Two image ads in two different looks and one voiceover video, from one headline and one script.",
     "parts": [{"scene": 0}, {"preset": "clean-headline", "else_scene": 1}, {"voiceover": True}],
     "fields": ("headline", "script")},
    {"id": "offer", "name": "Offer pack",
     "line": "Your saved price or offer as a badge, sized for the feed and for stories, with a clean headline image.",
     "parts": [{"preset": "offer", "aspect": "4:5"}, {"preset": "offer", "aspect": "9:16"},
               {"preset": "clean-headline", "aspect": "1:1"}],
     "fields": ("headline", "badge")},
    {"id": "marketplace", "name": "Marketplace set",
     "line": "Pictures for a listing: the product on pure white with no words, a close-up and one in a real setting.",
     "parts": [{"preset": "marketplace"}, {"preset": "closeup", "aspect": "1:1"}, {"scene": 0, "aspect": "1:1"}],
     "fields": ("headline",)},
]
PACK_BY_ID = {p["id"]: p for p in PACKS}
PACK_FIELDS = {
    "headline": {"label": "Headline", "max": 80, "hint": "The same words on each image."},
    "script": {"label": "Voiceover script", "max": 900, "hint": "What the narrator says. The length is fitted to it."},
    "badge": {"label": "Badge", "max": 28, "hint": "The price or the offer saved on this product, in its words."},
}


def _image_preset(pid):
    """An image preset Create can run right now (a text-layer one only while TEXT_LAYERS is on), else None."""
    pre = presets.get(pid)
    if pre is None or pre.tool != "ad_image" or pre.kind == "type":
        return None
    if pre.layout and not presets.text_layers_on():
        return None
    return pre


def pack_parts(pack) -> list[dict]:
    """A pack's parts as they can be made today (a missing text-layer preset falls back to a scene, or drops the pack)."""
    out = []
    for part in pack["parts"]:
        if part.get("preset"):
            if _image_preset(part["preset"]) is not None:
                out.append(part)
            elif "else_scene" in part:
                out.append({"scene": part["else_scene"], **({"aspect": part["aspect"]} if part.get("aspect") else {})})
            else:
                return []
        else:
            out.append(part)
    if any(p.get("voiceover") for p in out) and "voiceover" not in {m["id"] for m in available_modes()}:
        return []
    return out


def packs() -> list[dict]:
    """The packs on Workflow's shelf: what is inside each and what it starts at (the parts' own from-prices)."""
    prices, out = catalog.from_prices(), []
    for pack in PACKS:
        parts = pack_parts(pack)
        if not parts:
            continue
        inside, total = [], 0
        for part in parts:
            if part.get("voiceover"):
                inside.append({"kind": "video", "name": "Voiceover video", "aspect": "9:16"})
                total += prices["ugc_video"]
            else:
                pre = presets.get(part.get("preset")) if part.get("preset") else None
                inside.append({"kind": "image", "name": pre.name if pre is not None else "A look picked for your product",
                               "aspect": part.get("aspect") or (pre.aspect if pre is not None else "") or "4:5"})
                total += prices["ad_image"]
        n_img, n_vid = sum(1 for x in inside if x["kind"] == "image"), sum(1 for x in inside if x["kind"] == "video")
        counts = " + ".join(x for x in (f"{n_img} image ad{'s' if n_img != 1 else ''}" if n_img else "",
                                        f"{n_vid} voiceover video{'s' if n_vid != 1 else ''}" if n_vid else "") if x)
        out.append({"id": pack["id"], "name": pack["name"], "line": pack["line"], "inside": inside, "counts": counts,
                    "from": total, "fields": [dict(PACK_FIELDS[f], id=f) for f in pack["fields"]]})
    return out


def _first_run(conn) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (g.ws["id"],)).fetchone() is None


def scene_templates(conn, pid, pick_id=None, n=2) -> list[dict]:
    """The image templates that suit this product, best first, each with its scene and its own headline: the classic
    first run's choice (catalog.first_run_pair and first_run_category, the same order app.first_ad walks), skipping a
    later one whose scene the compliance rules refuse. `pick_id` puts a template the member chose in front."""
    row = products.get_product(conn, g.ws["id"], pid) if pid else None
    if row is None:
        return []
    info = {"product": row["name"], "description": row["description"] or "", "tags": row["tags"] or ""}
    b = create_routes.brand(row["id"])
    first = catalog.first_run_pair(info, pick_id)[0]
    order = [first] + [catalog.TEMPLATE_BY_ID[i] for i in catalog.first_run_category(info) if i != first["id"]]
    out = []
    for t in order:
        fill = catalog.template_prefill(t, b)
        scene = fill.get("body") or ""
        if out and not compliance.check(scene)["ok"]:
            continue
        out.append({"id": t["id"], "name": t["name"], "aspect": t.get("aspect") or "4:5", "scene": scene,
                    "headline": (fill.get("items") or [""])[0]})
        if len(out) >= n:
            break
    return out


def _scene_part(d, t, aspect="", text=None) -> dict:
    """One image ad from template `t` with the member's headline (the template's own when they wrote none)."""
    words = (d.get("text") if text is None else text) or ""
    payload = {"mode": "image", "product": d.get("product"), "text": str(words).strip() or t["headline"], "count": 1,
               "preset": t["id"], "scene": t["scene"], "aspect": aspect or t["aspect"],
               "subline": d.get("subline") or "", "cta": d.get("cta") or ""}
    return {"name": t["name"], "kind": "image", "payload": payload, "template": t["id"]}


def pair_parts(conn, d):
    """The first run's two free image ads as two DIFFERENT templates (None when this request isn't that): asked for
    with `pair`, an image ad, two of them, and nothing made in this workspace yet. A preset the member pinned stays
    the first of the two; the second is the next template that suits the product."""
    if not d.get("pair") or (MODE_BY_ID.get(d.get("mode")) or {}).get("type") != "image":
        return None
    if str(d.get("count")) != "2" or not _first_run(conn):
        return None
    pinned = _image_preset(d.get("preset"))
    aspect = str(d.get("aspect") or "")
    if pinned is not None:
        tpls = [t for t in scene_templates(conn, d.get("product"), n=3) if t["id"] != pinned.id][:1]
        if not tpls:
            return None
        mine = {k: v for k, v in d.items() if k not in ("pair", "total", "confirm_many")}
        mine.update(count=1, aspect=aspect or pinned.aspect or "4:5")
        return [{"name": pinned.name, "kind": "image", "payload": mine, "template": pinned.id},
                _scene_part(d, tpls[0], aspect)]
    tpls = scene_templates(conn, d.get("product"), n=2)
    if len(tpls) < 2:
        return None
    first = _scene_part(d, tpls[0], aspect)
    if _clean(d.get("scene"), 2000):
        first["payload"]["scene"] = _clean(d.get("scene"), 2000)       # a scene they typed is theirs: it leads
    return [first, _scene_part(d, tpls[1], aspect)]


def _pack_parts_for(conn, d) -> list[dict]:
    pack = PACK_BY_ID.get(str(d.get("pack") or ""))
    parts = pack_parts(pack) if pack else []
    if not parts:
        raise PackError("That pack isn't available right now. Pick another.")
    row = products.get_product(conn, g.ws["id"], d.get("product")) if d.get("product") else None
    if row is None:
        raise PackError("Pick a product first.")
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    headline = _clean(f.get("headline"), PACK_FIELDS["headline"]["max"])
    script = " ".join(str(f.get("script") or "").split())[:PACK_FIELDS["script"]["max"]]
    badge = _clean(f.get("badge"), PACK_FIELDS["badge"]["max"])
    if "headline" in pack["fields"] and not headline:
        raise PackError("Write the headline first.")
    if "script" in pack["fields"] and not script:
        raise PackError("Write the voiceover script first.")
    if "badge" in pack["fields"] and not badge:
        raise PackError("Write the badge first: the price or the offer saved on this product.")
    scenes = scene_templates(conn, row["id"], n=1 + max([p.get("scene", 0) for p in parts if "scene" in p] or [0]))
    out = []
    for part in parts:
        if part.get("voiceover"):
            out.append({"name": "Voiceover", "kind": "video",
                        "payload": {"mode": "voiceover", "product": row["id"], "text": script, "voice": d.get("voice") or "",
                                    "aspect": "9:16", "resolution": catalog.DEFAULT_RESOLUTION}})
        elif "scene" in part:
            if part["scene"] >= len(scenes):
                raise PackError("This product has no second look to offer yet. Pick another pack.")
            out.append(_scene_part({"product": row["id"]}, scenes[part["scene"]], part.get("aspect") or "", text=headline))
        else:
            pre = presets.get(part["preset"])
            layers = {"badge": badge} if any(x.get("id") == "badge" for x in pre.fields) else {}
            out.append({"name": pre.name, "kind": "image", "template": pre.id,
                        "payload": {"mode": "image", "product": row["id"], "text": headline, "count": 1, "preset": pre.id,
                                    "aspect": part.get("aspect") or pre.aspect or "4:5", "layers": layers}})
    return out


def parts_for(d):
    """The composer payloads this request stands for: None for an ordinary request (one Create config, as before),
    else a list of {"name", "kind", "payload"}. Raises PackError."""
    if not d.get("pack") and not d.get("pair"):
        return None
    with db.connect() as conn:
        if d.get("pack"):
            return _pack_parts_for(conn, d)
        return pair_parts(conn, d)


def _line_label(part, label) -> str:
    return f"{part['name']} · {label.split(' · ')[0]}" if part["kind"] == "image" else f"{part['name']} · {label}"


def quote_parts(parts) -> dict:
    """The price of every part, each from Create's own quote, as one answer in the same shape (plus `parts`, the
    total of each part in order, and `ads`, how many finished ads that is)."""
    lines, totals, count, ads, videos, bal = [], [], 0, 0, 0, db.balance(g.ws)
    for i, part in enumerate(parts):
        q = create_routes.quote_for(config_for(part["payload"])).get_json() or {}
        if q.get("error") or not q.get("count"):
            return {"error": f"{part['name']}: {q.get('error') or 'that can not be priced. Check it and try again.'}",
                    "step": q.get("step", ""), "count": 0, "total": 0, "balance": q.get("balance", bal)}
        totals.append(q["total"])
        count += q["count"]
        bal = q["balance"]
        ads += q["count"] * (int(part["payload"].get("count") or 1) if part["kind"] == "image" else 1)
        videos += q["count"] if q.get("videos") else 0
        lines += [{"key": f"{i}|{ln['key']}", "label": _line_label(part, ln["label"]), "hook": ln["hook"],
                   "credits": ln["credits"], "on": True} for ln in q["lines"] if ln["on"]]
    total = sum(totals)
    return {"count": count, "ads": ads, "total": total, "each": 0, "every": count, "videos": bool(videos),
            "warn": videos > create_routes.WARN_VIDEOS, "confirm": videos > create_routes.CONFIRM_VIDEOS,
            "max": create_routes.MAX_BATCH, "balance": bal, "short": max(0, total - bal),
            "pack": create_routes.suggest_pack(total - bal), "lines": lines, "parts": totals,
            "names": [p["name"] for p in parts]}


def _merge(bids, title, extra) -> int:
    """One batch for what one confirmation made: the later parts' jobs move into the first batch (so Recent, the
    highlight and the "your ads are ready" email see one thing). Only batches this request just made are touched."""
    first, rest = bids[0], bids[1:]
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id=? AND workspace_id=?", (first, g.ws["id"])).fetchone()
        if b is None:
            return first
        cfg = json.loads(b["config_json"] or "{}")
        cfg.update(extra or {})
        credits = b["credits"]
        if rest:
            marks = ",".join("?" * len(rest))
            credits += conn.execute(f"SELECT COALESCE(SUM(credits), 0) FROM batches WHERE workspace_id=? AND id IN ({marks})",
                                    (g.ws["id"], *rest)).fetchone()[0]
            conn.execute(f"UPDATE jobs SET batch_id=? WHERE workspace_id=? AND batch_id IN ({marks})",
                         (first, g.ws["id"], *rest))
            conn.execute(f"DELETE FROM batches WHERE workspace_id=? AND id IN ({marks})", (g.ws["id"], *rest))
        conn.execute("UPDATE batches SET title=?, credits=?, config_json=? WHERE id=?",
                     ((title or b["title"] or "")[:80], credits, json.dumps(cfg), first))
        conn.commit()
    return first


def _name_jobs(bid, name):
    """A part's image jobs carry the look's name on their cards ("Gift idea · 1:1"), as the classic first run's do.
    One statement in the database, so nothing the worker writes meanwhile is lost; skipped where SQLite has no JSON."""
    import sqlite3
    try:
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET params_json=json_set(params_json, '$.label', ? || COALESCE(' · ' || "
                         "json_extract(params_json, '$.aspect'), '')) WHERE batch_id=? AND workspace_id=?",
                         (name, bid, g.ws["id"]))
            conn.commit()
    except sqlite3.Error as e:
        log.warning("could not name the jobs of batch %s: %s", bid, str(e)[:120])


def generate_parts(parts, d, title, extra):
    """Start every part, each through create_routes.generate_for with the part's own total, after checking that the
    sum is the total the member confirmed and that their credits cover all of it. Videos go first, so a step Create
    asks for before a video (confirming the email) is asked before anything has started. If a later part is refused
    the earlier ones stay, only they are charged, and the member is told which one didn't start."""
    q = quote_parts(parts)
    if q.get("error"):
        return jsonify({"error": q["error"], "step": q.get("step", "")}), 400
    if str(q["total"]) != str(d.get("total")):
        return jsonify({"error": f"The total changed to {q['total']:,} credits. Check it and confirm again.",
                        "total": q["total"]}), 409
    if q["short"]:
        return jsonify({"error": f"That needs {q['total']:,} credits and you have {q['balance']:,}. Pick fewer or top up "
                                 "on Billing.", "billing": url_for("billing_page"), "pack": q["pack"]}), 402
    if q["confirm"] and not d.get("confirm_many"):
        return jsonify({"error": f"That's {q['count']} videos ({q['total']:,} credits). Tick the box to confirm you "
                                 "want all of them.", "confirm": True}), 400
    order = sorted(range(len(parts)), key=lambda i: (parts[i]["kind"] == "image", i))
    started, queued, note = {}, 0, ""
    for i in order:
        out = create_routes.generate_for(config_for(dict(parts[i]["payload"], total=q["parts"][i], confirm_many=True)))
        resp, status = out if isinstance(out, tuple) else (out, 200)
        data = resp.get_json(silent=True) or {}
        if status != 200 or not data.get("ok"):
            if not started:
                return out                  # nothing has started: Create's own answer (the email step, a hold, a rule)
            note = f"{parts[i]['name']} didn't start, so it wasn't charged: {data.get('error') or 'make it again from Studio.'}"
            break
        started[i] = data["batch"]
        queued += data.get("queued") or 0
        if parts[i]["kind"] == "image":
            _name_jobs(data["batch"], parts[i]["name"])
    bid = _merge([started[i] for i in sorted(started)], title, extra)
    if note:
        flash(note)
    return jsonify({"ok": True, "batch": bid, "queued": queued, "note": note, "url": url_for("studio_v2.page", made=bid)})


def _parts_or_error(d):
    """(parts or None, an error response or None) for a Studio request."""
    try:
        return parts_for(d), None
    except PackError as e:
        return None, jsonify({"error": str(e), "count": 0, "total": 0, "balance": db.balance(g.ws)})


def _title_for(d, parts) -> tuple[str, dict]:
    if d.get("pack"):
        pack = PACK_BY_ID[d["pack"]]
        return pack["name"], {"pack": pack["id"], "templates": [p.get("template") or p["payload"]["mode"] for p in parts]}
    return "Your first ads", {"templates": [p["template"] for p in parts]}


@bp.route("/app/studio/quote", methods=["POST"])
@login_required
def quote():
    if not ui_v2.on():
        return _off()
    d = _payload()
    parts, err = _parts_or_error(d)
    if err is not None:
        return err
    if parts is None:
        return create_routes.quote_for(config_for(d))
    return jsonify(quote_parts(parts))


@bp.route("/app/studio/claims", methods=["POST"])
@login_required
def claims():
    if not ui_v2.on():
        return _off()
    d = _payload()
    parts, err = _parts_or_error(d)
    if err is not None:
        return err
    if parts is None:
        return create_routes.claims_for(config_for(d))
    out = None
    for part in parts:              # the same words are checked once: Create keeps each result by what was checked
        out = create_routes.claims_for(config_for(part["payload"]))
        resp, status = out if isinstance(out, tuple) else (out, 200)
        if status != 200 or (resp.get_json(silent=True) or {}).get("status") == "flagged":
            return out
    return out


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
    parts, err = _parts_or_error(d)
    if err is not None:
        return err.get_json(), 400
    if parts is not None:
        title, extra = _title_for(d, parts)
        return generate_parts(parts, d, title, extra)
    out = create_routes.generate_for(config_for(d))
    resp, status = out if isinstance(out, tuple) else (out, 200)
    if status == 200:
        data = resp.get_json() or {}
        if data.get("ok"):
            data["url"] = url_for("studio_v2.page", made=data.get("batch"))
            return jsonify(data)
    return out


# ---------------------------------------------------------------- presenters (the picker's roster)

_AGE = re.compile(r"\b(\d)0s\b")


def _setting_label(text: str) -> str:
    """"a bright, lived-in kitchen" -> "Bright, lived-in kitchen"."""
    t = re.sub(r"^(a|an|the)\s+", "", (text or "").strip(), flags=re.I)
    return t[:1].upper() + t[1:]


def fallback_roster() -> list[dict]:
    """Today's eight presenters (catalog.PRESENTERS) in the shape presenters.roster() has: one situation each, the
    setting they are described in; the sample still as the portrait when one exists; no voice sample."""
    out = []
    for p in catalog.PRESENTERS:
        v = catalog.VOICE_BY_ID[catalog.default_voice(p["id"])]
        age = _AGE.search(p["look"])
        still = presenter_stills.photo_url(p["id"], url_for)
        out.append({"id": p["id"], "name": p["name"], "age_band": f"{age.group(1)}0s" if age else "",
                    "gender": "female" if re.search(r"\bwoman\b", p["look"]) else "male" if re.search(r"\bman\b", p["look"]) else "",
                    "look": p["look"], "voice": {"id": v["id"], "label": v["name"]}, "portrait": still,
                    "voice_sample": None, "vibe": p["vibe"],
                    "situations": [{"id": "home", "label": _setting_label(p["setting"]), "still": still}]})
    return out


def roster() -> list[dict]:
    """Who the presenter picker offers. presenters.roster() when that module is there (id, name, age_band, gender,
    look, voice {id, label}, portrait, voice_sample, situations [{id, label, still}]); today's eight otherwise, and
    also when it answers with nothing or fails. Every value is cleaned for the page: a missing key is empty."""
    rows = None
    try:
        import presenters
        rows = presenters.roster()
    except ImportError:
        rows = None
    except Exception as e:  # noqa: BLE001  (the picker must open whatever that module does)
        log.warning("presenters.roster() failed, using the built-in presenters: %s", str(e)[:160])
    if not rows:
        rows = fallback_roster()
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        voice = r.get("voice") if isinstance(r.get("voice"), dict) else {"id": "", "label": str(r.get("voice") or "")}
        sits = [{"id": str(x.get("id")), "label": str(x.get("label") or x.get("id")), "still": x.get("still") or ""}
                for x in (r.get("situations") or []) if isinstance(x, dict) and x.get("id")]
        out.append({"id": str(r["id"]), "name": str(r.get("name") or r["id"]), "age_band": str(r.get("age_band") or ""),
                    "gender": str(r.get("gender") or ""), "look": str(r.get("look") or ""),
                    "vibe": str(r.get("vibe") or ""),
                    "voice": {"id": str(voice.get("id") or ""), "label": str(voice.get("label") or "")},
                    "portrait": r.get("portrait") or "", "voice_sample": r.get("voice_sample") or "", "situations": sits})
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


_VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")


def example_of(preset_id: str, fallback: str) -> dict:
    """{"preview", "preview_kind"} for a gallery card: a real output of this preset when the library has one
    (presets.example_url, added by the back end; any failure or an empty answer keeps today's placeholder)."""
    url = ""
    fn = getattr(presets, "example_url", None)
    if callable(fn):
        try:
            url = fn(preset_id) or ""
        except Exception as e:  # noqa: BLE001
            log.warning("presets.example_url(%s) failed: %s", preset_id, str(e)[:120])
    url = str(url or "")
    return {"preview": url or fallback, "example": bool(url),
            "preview_kind": "video" if url.split("?", 1)[0].lower().endswith(_VIDEO_EXT) else "image"}


def uses_of(p) -> list[str]:
    """A preset's `use` tag or tags ("launch", "offer", ...) when its record has any, as a list."""
    use = getattr(p, "use", None) or (getattr(p, "extra", None) or {}).get("use")
    if isinstance(use, str):
        use = [use]
    return [str(u).strip() for u in (use or []) if str(u).strip()][:4]


def use_label(tag: str) -> str:
    return tag.replace("_", " ").replace("-", " ").strip().capitalize()


def gallery(product=None) -> list[dict]:
    """Everything "More" offers: the ad types, then the presets that fit this product. `opens` says what picking
    it does: "mode" switches the composer, "preset" pins it on an image ad, "scene" fills a product video's
    scene, "advanced" opens Create (a reference clip can't be picked in the composer). `uses` are the preset's own
    use tags (the pills filter by them when any preset has one)."""
    out = []
    prices = catalog.from_prices()
    for a in catalog.ad_types():
        pre = presets.get(a["id"])
        out.append({"id": "type:" + a["id"], "name": a["name"], "what_for": a["line"], "group": "type",
                    "badge": "Ad type", "from": prices.get(a["tool"], 0),
                    **example_of(a["id"], url_for("static", filename=a["preview"] + ".jpg")),
                    "uses": uses_of(pre) if pre is not None else [],
                    "opens": "mode" if a["id"] in MODE_OF_TYPE else "advanced", "mode": MODE_OF_TYPE.get(a["id"], ""),
                    "href": url_for("create.index", type=a["id"]) if a["id"] not in MODE_OF_TYPE else ""})
    b = create_routes.brand(product["id"]) if product is not None else {}
    for p in presets.library_for(product):
        c = presets.card(p, url_for)
        item = {"id": p.id, "name": p.name, "what_for": p.what_for or c["line"], "from": c["from"],
                "aspect": p.aspect, "uses": uses_of(p), **example_of(p.id, c["preview"])}
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


def gallery_pills(gal) -> list[dict]:
    """The filter pills over "More": the presets' own use tags when records have them, else the three groups."""
    tags = []
    for x in gal:
        for u in x.get("uses") or []:
            if u not in tags:
                tags.append(u)
    if tags:
        return [{"by": "use", "id": t, "label": use_label(t)} for t in tags]
    return [{"by": "group", "id": "type", "label": "Ad types"}, {"by": "group", "id": "image", "label": "Image presets"},
            {"by": "group", "id": "video", "label": "Video presets"}]


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
        # the first run's two free image ads are two different looks (the classic first run's own choice)
        pair = scene_templates(conn, active["id"], n=2) if (active is not None and not ever) else []
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
    one_image = catalog.price_credits("ad_image", batch=1)
    pre_req = presets.get(request.args.get("preset") or "")
    if pre_req is not None and not prefill:
        prefill = {"pick": pre_req.id}
    talk = catalog.TOOLS["ugc_video"]
    pres = roster()
    boot = {
        "csrf": _csrf(), "modes": modes, "mode": mode, "note": note, "balance": bal, "first_run": first_run,
        "free_images": free_images, "products": plist, "product": active["id"] if active is not None else None,
        "presenters": pres, "max_presenters": create_routes.MAX_PRESENTERS,
        "voices": [{"id": v["id"], "name": v["name"], "who": v.get("who") or ""} for v in catalog.VOICES],
        "durations": {"talk": talk["durations"], "video": catalog.TOOLS["photo_to_video"]["durations"]},
        "aspects": {"talk": talk.get("aspects") or [], "image": catalog.TOOLS["ad_image"]["aspects"]},
        "resolutions": {"talk": list(catalog.resolutions("ugc_video")), "video": list(catalog.resolutions("photo_to_video"))},
        "max_count": MAX_COUNT, "gallery": gal, "gallery_pills": gallery_pills(gal), "prefill": prefill,
        # first run: 2 image ads are made as two different templates when the credits cover two single ads
        "pair": first_run and bal >= 2 * one_image,
        "pair_names": [t["name"] for t in pair] if len(pair) == 2 else [],
        "claims_check": claims_check.on() and llm.anthropic_ready(),
        "claims_credits": claims_check.estimate() if claims_check.on() and llm.anthropic_ready() else 0,
        "words": {str(d): catalog.hard_words(d) for d in talk["durations"]},
        "website": answers.get("website") or "",
        "urls": {"quote": url_for("studio_v2.quote"), "generate": url_for("studio_v2.generate"),
                 "claims": url_for("studio_v2.claims"), "import": url_for("start_import"),
                 "switch": url_for("products.switch"), "new_product": url_for("products.new"),
                 "billing": url_for("billing_page"), "studio": url_for("studio_v2.page"),
                 "advanced": url_for("create.index"), "workflow": url_for("studio_v2.workflow")},
    }
    return render_template("v2/studio.html", boot=boot, jobs=jobs, outs=outs, verdicts=verdicts, prices=prices, eta=eta,
                           names=names, recents=recents, templates=catalog.TEMPLATE_BY_ID, tools=catalog.DISPLAY_TOOLS,
                           made=request.args.get("made", type=int), free_fix_on=free_fix.on(),
                           ad_type_names=catalog.AD_TYPE_BY_ID, first_run=first_run, modes=modes)


def _csrf():
    from core import csrf_token
    return csrf_token()


def starter_script(product, words=None) -> str:
    """A first draft of a voiceover from the product's own saved description: its name, then whole sentences while
    they fit a short video. Nothing is written by AI here; the member edits it before anything is priced."""
    if product is None:
        return ""
    limit = words or catalog.max_words(catalog.TOOLS["ugc_video"]["durations"][min(2, len(catalog.TOOLS["ugc_video"]["durations"]) - 1)])
    desc = " ".join(str(product["description"] or "").split())
    if not desc:
        return ""
    out = [product["name"].rstrip(".") + "."]
    for sent in re.split(r"(?<=[.!?])\s+", desc):
        if len(" ".join(out + [sent]).split()) > limit:
            break
        out.append(sent)
    return " ".join(out) if len(out) > 1 else ""


@bp.route("/app/studio/workflow")
@login_required
def workflow():
    """Workflow: describe a campaign (handed to the planner's own form, with its price note) or start from a pack
    of presets (priced and confirmed like anything else in Studio)."""
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import campaign_budget
    import planner
    with db.connect() as conn:
        recents = conn.execute("SELECT b.id, b.title, b.ad_type, b.created_at, "
                               "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id AND j.status IN ('pending','running')) busy "
                               "FROM batches b WHERE b.workspace_id=? ORDER BY b.id DESC LIMIT 8", (g.ws["id"],)).fetchall()
        active = products.get_product(conn, g.ws["id"], g.ws["product_id"]) if g.ws["product_id"] else None
        scenes = scene_templates(conn, active["id"], n=2) if active is not None else []
        first_run = _first_run(conn)
    shelf = packs()
    talk = catalog.TOOLS["ugc_video"]
    offer = ((active["offer"] if "offer" in active.keys() else "") or active["price"] or "") if active is not None else ""
    boot = {
        "csrf": _csrf(), "workflow": True, "balance": db.balance(g.ws), "first_run": first_run,
        "product": active["id"] if active is not None else None, "packs": shelf,
        "prefill": {"headline": scenes[0]["headline"] if scenes else "", "script": starter_script(active), "badge": offer},
        "voices": [{"id": v["id"], "name": v["name"]} for v in catalog.VOICES],
        "words": {str(d): catalog.hard_words(d) for d in talk["durations"]}, "durations": {"talk": talk["durations"]},
        "claims_check": claims_check.on() and llm.anthropic_ready(),
        "claims_credits": claims_check.estimate() if claims_check.on() and llm.anthropic_ready() else 0,
        "urls": {"quote": url_for("studio_v2.quote"), "generate": url_for("studio_v2.generate"),
                 "claims": url_for("studio_v2.claims"), "billing": url_for("billing_page"),
                 "studio": url_for("studio_v2.page")},
    }
    return render_template("v2/workflow.html", boot=boot, recents=recents, active=active, packs=shelf,
                           plan_cap=planner.cap_credits(), plan_on=llm.anthropic_ready(),
                           plan_platforms=campaign_budget.platforms_for("sales"))


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

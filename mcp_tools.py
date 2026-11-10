"""Campaignify's MCP tools and the adapter that ties mcpserver.py to this app.

Everything that writes or spends credits calls the app's own route
(ToolContext.call): Create's quote and generate (generate refuses unless the
total matches the quote the member saw, which is what confirm_total_credits
carries), the Copy studio, the campaign planner and Competitor Snapshot. So
plan features, credit checks, compliance rules, email confirmation for
videos and every other gate are the web app's, word for word. Reads go through
the app's routes where one exists and otherwise read this workspace's rows the
way the Library and report pages do. Costs are in credits only.
Docs for customers: /docs/mcp. Switch: MCP_SERVER=on.
"""
import json
import re

from flask import current_app, g

import catalog
import competitors
import copywriter
import core
import db
import library
import mcpserver
import planner
import platforms
import plans
import presenters
import presets
import products
from mcpserver import PAGE_PROPS, Tool, ToolError, paginate

SPOKEN, IMAGE, SCENE = ("ugc", "voiceover"), ("image",), ("product_video",)


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _need(feature):
    """The plan check up front, for a clear message (the route checks it again)."""
    if not plans.has(g.ws, feature):
        need = plans.required(feature)
        raise ToolError(f"{plans.FEATURES[feature]['name']} is on the {need['name']} plan and up. "
                        "See plans on the Billing page.")


def _balance():
    with db.connect() as conn:
        return db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone())


def _created_id(r, pattern):
    """The new row's id from the route's redirect (/app/copy/12 and the like),
    or the route's own message when it sent the member back instead."""
    m = re.search(pattern, r.location or "")
    if not m:
        raise ToolError(r.message() if (r.flashes or r.data) else "That didn't start. Nothing was charged.")
    return int(m.group(1))


# ---------------------------------------------------------------- reads

def account_status(ctx, args):
    with ctx.as_user():
        ws, p = g.ws, plans.plan(g.ws)
        feats = {k: plans.has(ws, k) for k in ("studio", "copy", "planner", "competitors")}
        import verify
        with db.connect() as conn:
            unconfirmed = bool(verify.blocked(conn, ws, g.user))
        return {"workspace": {"id": ws["id"], "name": ws["name"]}, "plan": {"code": plans.plan_code(ws), "name": p["name"]},
                "credits": db.balance(ws), "features": feats, "email_confirmed": not unconfirmed,
                "note": "Videos need a confirmed email." if unconfirmed else None,
                "packs": [{"name": x["name"], "credits": x["credits"]} for x in catalog.PACKS]}


def list_products(ctx, args):
    with ctx.as_user() as conn:
        tree = products.tree(conn, g.ws)
        out = []
        for b in tree:
            for pr in b["products"]:
                out.append({"product_id": pr["id"], "name": pr["name"], "brand_id": b["brand"]["id"],
                            "brand": b["brand"]["brand_name"], "brand_locked": b["locked"],
                            "photos": len(products.photos_for(conn, g.ws["id"], pr["id"])),
                            "active": pr["id"] == g.ws["product_id"]})
        brands = [{"brand_id": b["brand"]["id"], "name": b["brand"]["brand_name"], "locked": b["locked"],
                   "products": len(b["products"])} for b in tree]
    res = paginate(out, args, key="products")
    res["brands"] = brands
    res["note"] = "A locked brand is over this plan's brand limit. Videos need at least 1 product photo."
    return res


def get_product(ctx, args):
    st = ctx.must("GET", "/app/create/product", query={"id": args["product_id"]}).data
    st.pop("photos", None)
    with ctx.as_user() as conn:
        st["photo_ids"] = [x["id"] for x in products.photos_for(conn, g.ws["id"], args["product_id"])]
    return st


def _site_admin(ctx) -> bool:
    """Is this token's user one of Campaignify's own admins? Read from the users table on every call (adapter.enter
    never gives a token admin powers in the app: g.user.is_admin stays 0)."""
    with db.connect() as conn:
        r = conn.execute("SELECT is_admin FROM users WHERE id=? AND workspace_id=?",
                         (ctx.user_id, ctx.account_id)).fetchone()
    return bool(r and r["is_admin"])


def _preset_row(ctx, p) -> dict:
    """One preset for list_ad_options: what it is for, its tags, its price and its example."""
    url = lambda u: (ctx.base_url + u) if u else None  # noqa: E731
    d = {"id": p.id, "name": p.name, "what_for": p.what_for, "kind": p.kind, "format": p.format,
         "ad_type": p.ad_type, "aspect": p.aspect or None,
         "tags": {"category": list(p.category), "use": list(p.use)}, "from_credits": p.price_credits(),
         "example_image": url(presets.example_url(p.id)), "example_video": url(presets.example_video_url(p.id)),
         "needs": list(p.inputs)}
    if p.layout:
        d["layers"] = [{k: f[k] for k in ("id", "label", "type", "n", "max", "required", "hint") if k in f}
                       for f in p.fields]
    if p.kind == "recipe":
        d.update(seconds=p.price.get("duration"), situation=p.extra.get("situation") or None,
                 script=(p.extra.get("script") or {}).get("shape"),
                 beats=[{k: b[k] for k in ("beat", "seconds", "shot", "say", "text")} for b in p.beats])
        if not presets.runnable(p):
            d["not_available_yet"] = p.needs
    return d


def options(ctx, args):
    with ctx.as_user():
        types = [{"id": a["id"], "name": a["name"], "what": a["line"]} for a in catalog.ad_types() if a["id"] != "motion"]
    tools = {t: {"aspects": catalog.TOOLS[t].get("aspects"), "durations": catalog.TOOLS[t].get("durations"),
                 "resolutions": list(catalog.resolutions(t)) or None} for t in ("ad_image", "ugc_video", "photo_to_video")}
    url = lambda u: (ctx.base_url + u) if u else None  # noqa: E731
    roster = [dict(p, portrait=url(p["portrait"]), voice_sample=url(p["voice_sample"]),
                   situations=[dict(x, still=url(x["still"])) for x in p["situations"]]) for p in presenters.roster()]
    return {"ad_types": types, "formats": {"image": tools["ad_image"], "ugc": tools["ugc_video"],
                                           "voiceover": tools["ugc_video"], "product_video": tools["photo_to_video"]},
            "presenters": [{"id": p["id"], "name": p["name"], "vibe": p["vibe"], "look": p["look"]}
                           for p in presenters.all_presenters()],
            "voices": [{"id": v["id"], "name": v["name"], "who": v["who"]} for v in catalog.VOICES],
            # m15: the preset library (each with its tags and example) and the presenter roster (with situations)
            "presets": [_preset_row(ctx, p) for p in presets.offered(None, everything=_site_admin(ctx))
                        if p.tool != "motion_copy"],
            "preset_uses": presets.USES,
            "roster": roster,
            "situations": [{"id": k, "label": v["label"]} for k, v in presenters.SITUATIONS.items()],
            "copy_kinds": list(platforms.KINDS), "copy_platforms": list(platforms.PLATFORMS),
            "note": "AI presenters are generated people, never real ones. Words per video: about "
                    f"{catalog.max_words(10)} for 10 seconds. A preset is passed to quote_ad_batch and the generate "
                    "tools as `preset`; a presenter video also takes `presenter` (or `presenters`) and `situation`."}


def quote_ai(ctx, args):
    with ctx.as_user():
        return {"credits_available": db.balance(g.ws),
                "ad_copy": {k: {"3_variants": copywriter.estimate(3, k), "5_variants": copywriter.estimate(5, k)}
                            for k in platforms.KINDS},
                "campaign_plan_max": planner.cap_credits(),
                "competitor_snapshot_max": {str(n) if n else "find_them": c for n, c in competitors.quotes().items()},
                "note": "AI writing is charged at what it uses, up to these amounts. Ad images and videos: use "
                        "quote_ad_batch."}


ASSET_TYPES = {"image": ("ad_image",), "video": ("photo_to_video", "motion_copy"), "talking": ("ugc_video",)}


def list_assets(ctx, args):
    where, q = ["j.workspace_id=?", "j.deleted_at IS NULL"], [ctx.account_id]
    if args.get("product_id"):
        where.append("j.product_id=?")
        q.append(args["product_id"])
    if args.get("batch_id"):
        where.append("j.batch_id=?")
        q.append(args["batch_id"])
    if args.get("type"):
        t = ASSET_TYPES[args["type"]]
        where.append(f"j.tool IN ({','.join('?' * len(t))})")
        q.extend(t)
    if args.get("status"):
        st = {"done": ("done",), "making": ("pending", "running"), "failed": ("failed",)}[args["status"]]
        where.append(f"j.status IN ({','.join('?' * len(st))})")
        q.extend(st)
    with ctx.as_user() as conn:
        jobs = conn.execute(f"SELECT j.* FROM jobs j WHERE {' AND '.join(where)} ORDER BY j.id DESC LIMIT 200", q).fetchall()
        ids = [j["id"] for j in jobs]
        outs = {}
        if ids:
            for o in conn.execute(f"SELECT id, job_id, kind FROM outputs WHERE job_id IN ({','.join('?' * len(ids))}) "
                                  "ORDER BY id", ids):
                outs.setdefault(o["job_id"], []).append({"output_id": o["id"], "kind": o["kind"]})
        names = {p["id"]: p["name"] for p in products.products(conn, ctx.account_id)}
        lib = {a["job_id"]: a for a in library.assets(conn, ctx.account_id)} if '"library"' in "".join(
            j["params_json"] or "" for j in jobs) else {}
    items = []
    for j in jobs:
        prm = json.loads(j["params_json"] or "{}")
        item = {"job_id": j["id"], "type": catalog.job_ad_type(j["tool"], prm), "status": j["status"],
                "label": prm.get("label"), "hook": prm.get("hook"), "product": names.get(j["product_id"]),
                "batch_id": j["batch_id"], "credits": j["credits"], "created_at": j["created_at"],
                "error": j["error"] if j["status"] == "failed" else None, "outputs": outs.get(j["id"], [])}
        if j["id"] in lib:          # a library job (a site admin's own): each file with the name the export gives it
            a = lib[j["id"]]
            item["library"] = {k: a[k] for k in ("kind", "presenter", "situation", "preset", "demo") if a[k]}
            item["outputs"] = [dict(o) for o in a["outputs"]]
        items.append(item)
    res = paginate(items, args, key="assets")
    res["note"] = "Use get_asset_download_url with an output_id. The list holds the newest 200."
    return res


def download_link(ctx, args):
    with ctx.as_user() as conn:
        o = conn.execute("SELECT o.id, o.kind FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND "
                         "j.workspace_id=? AND j.deleted_at IS NULL", (args["output_id"], ctx.account_id)).fetchone()
    if not o:
        raise ToolError("Not found in this workspace.")
    return dict(ctx.file_link("output", o["id"]), output_id=o["id"], kind=o["kind"],
                note="A private link to this file. Anyone with the link can download it until it expires.")


def get_batch(ctx, args):
    with ctx.as_user() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id=? AND workspace_id=?", (args["batch_id"], ctx.account_id)).fetchone()
        if not b:
            raise ToolError("Not found in this workspace.")
    res = list_assets(ctx, {"batch_id": b["id"], "limit": 100})
    done = sum(1 for a in res["assets"] if a["status"] == "done")
    return {"batch_id": b["id"], "title": b["title"], "ad_type": b["ad_type"], "credits": b["credits"],
            "created_at": b["created_at"], "ready": done, "total": res["total"], "assets": res["assets"]}


# ---------------------------------------------------------------- ads: quote and generate

def _ad_config(args):
    d = {"type": args["ad_type"], "product": args["product_id"], "items": args["lines"]}
    for src, dst in (("body", "body"), ("presenters", "presenters"), ("voices", "voices"), ("voice", "voice"),
                     ("duration", "duration"), ("aspects", "aspects"), ("resolution", "resolution"),
                     ("versions", "versions"), ("photo_ids", "photos"), ("subline", "subline"), ("cta", "cta"),
                     ("setting", "setting"), ("placement", "placement"), ("audio", "audio"), ("only", "lines"),
                     ("preset", "preset"), ("situation", "situation"), ("layers", "layers")):
        if args.get(src) not in (None, "", []):
            d[dst] = args[src]
    if args.get("presenter") and not d.get("presenters"):        # one presenter (with `situation`), or the list
        d["presenters"] = [args["presenter"]]
    if args.get("title"):
        d["angle"] = {"title": args["title"]}
    return d


def _with_preset(ctx, d: dict) -> dict:
    """A config that names a preset, completed from the preset's own record: an image preset brings its scene and
    shape (the caller still writes the headline), a video preset its length. The route validates the rest."""
    if not d.get("preset"):
        return d
    pre = presets.get(d["preset"])
    if pre is None or pre.kind == "type":
        raise ToolError("Unknown preset. list_ad_options lists them.")
    if pre.kind == "recipe":
        if not presets.runnable(pre):
            raise ToolError(f"{pre.name} isn't available yet.")
        if d["type"] != pre.ad_type:
            raise ToolError(f"{pre.name} is made with ad_type {pre.ad_type}.")
        d.setdefault("duration", pre.price.get("duration") or 8)
    elif pre.tool == "ad_image":
        if d["type"] != "image":
            raise ToolError(f"{pre.name} is an image preset: use ad_type image.")
        with ctx.as_user():
            try:
                fill = catalog.template_prefill(pre.as_template(), core.brand(d.get("product")))
            except Exception:  # noqa: BLE001 — an unknown product: the route says so
                fill = {}
        d.setdefault("body", fill.get("body") or "")
        d.setdefault("aspects", fill.get("aspects") or ([pre.aspect] if pre.aspect else []))
    else:
        d.pop("preset")          # an older video template: its scene is the line the caller wrote
    return d


def quote_batch(ctx, args):
    q = ctx.must("POST", "/app/create/quote", json_body=_with_preset(ctx, _ad_config(args))).data
    if q.get("error"):
        raise ToolError(q["error"])
    pack = q.get("pack")
    return {"total_credits": q["total"], "ads": q["count"], "each_credits": q["each"], "credits_available": q["balance"],
            "short_by": q["short"], "videos": q["videos"], "needs_confirm_many": bool(q.get("confirm")),
            "lines": [{"key": x["key"], "label": x["label"], "hook": x["hook"], "credits": x["credits"],
                       "on": x["on"]} for x in q["lines"]],
            "pack_to_cover": {"name": pack["name"], "credits": pack["credits"]} if pack else None,
            "next": "To make these, call the generate tool with the same arguments and "
                    f"confirm_total_credits={q['total']}. Ask the user first."}


def generate(ctx, args, kinds):
    if args["ad_type"] not in kinds:
        raise ToolError(f"ad_type must be one of {', '.join(kinds)} for this tool.")
    d = _with_preset(ctx, _ad_config(args))
    d["total"] = args["confirm_total_credits"]
    if args.get("confirm_many_videos"):
        d["confirm_many"] = True
    r = ctx.call("POST", "/app/create/generate", json_body=d)
    if not r.ok:
        msg = ctx.adapter.refusal(r)
        if r.status == 409:
            msg += " Run quote_ad_batch again and confirm the new total with the user."
        raise ToolError(msg)
    return {"batch_id": r.data["batch"], "queued": r.data["queued"], "credits_charged": args["confirm_total_credits"],
            "note": "Making them now. Images take about a minute, videos a few minutes. Check with get_batch.",
            "url": ctx.base_url + r.data["url"]}


def _ad_schema(kinds, generating):
    props = {
        "ad_type": {"type": "string", "enum": list(kinds),
                    "description": "ugc = an AI presenter says your script to camera; voiceover = your product on "
                                   "screen with a narrator; image = finished image ads; product_video = a 5 or 10 "
                                   "second clip from one product photo."},
        "product_id": {"type": "integer", "minimum": 1, "description": "From list_brands_products."},
        "lines": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string", "maxLength": 900},
                  "description": "Spoken scripts (ugc, voiceover), headlines (image) or scenes (product_video); 1 to 5. "
                                 "Each makes its own ads."},
        "body": {"type": "string", "maxLength": 2000, "description": "Image: the concept. Spoken: words said after "
                                                                     "each line."},
        "title": {"type": "string", "maxLength": 80, "description": "A name for the batch (the angle)."},
        "presenters": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": list(catalog.PRESENTER_BY_ID)},
                       "description": "ugc: 1 to 4 AI presenters (from list_ad_options)."},
        "presenter": {"type": "string", "enum": list(catalog.PRESENTER_BY_ID),
                      "description": "ugc: one AI presenter (instead of `presenters`). From list_ad_options' roster."},
        "situation": {"type": "string", "enum": list(presenters.SITUATIONS),
                      "description": "ugc: where the presenter is (kitchen, car, outdoor, desk, sofa, bathroom-mirror, "
                                     "gym, shop-floor). The roster lists each presenter's own."},
        "preset": {"type": "string", "maxLength": 60,
                   "description": "A preset id from list_ad_options (an image preset with ad_type image; a video "
                                  "preset with its own ad_type). It supplies the scene or the shot; you write the "
                                  "lines."},
        "layers": {"type": "object",
                   "description": "Image presets with a text layer: their extra words, keyed by the preset's "
                                  "`layers` ids in list_ad_options (for example {\"badge\": \"$24\"})."},
        "voices": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": list(catalog.VOICE_BY_ID)},
                   "description": "voiceover: narrator voices."},
        "voice": {"type": "string", "enum": list(catalog.VOICE_BY_ID), "description": "ugc: override the presenter's voice."},
        "duration": {"type": "integer",
                     "enum": sorted(set(catalog.TOOLS["ugc_video"]["durations"]) | set(catalog.TOOLS["photo_to_video"]["durations"])),
                     "description": "Seconds. ugc and voiceover: " + ", ".join(map(str, catalog.TOOLS["ugc_video"]["durations"]))
                                    + "; product_video: " + " or ".join(map(str, catalog.TOOLS["photo_to_video"]["durations"]))
                                    + "."},
        "aspects": {"type": "array", "maxItems": 3, "items": {"type": "string", "enum": ["9:16", "1:1", "4:5", "16:9"]}},
        "resolution": {"type": "string", "enum": ["720p", "1080p"], "description": "Videos only."},
        "versions": {"type": "integer", "minimum": 1, "maximum": 4, "description": "Image: versions per headline."},
        "photo_ids": {"type": "array", "maxItems": 10, "items": {"type": "integer"},
                      "description": "Product photos to use (default: the product's photos)."},
        "subline": {"type": "string", "maxLength": 120}, "cta": {"type": "string", "maxLength": 40},
        "setting": {"type": "string", "maxLength": 120, "description": "ugc: where the presenter is."},
        "placement": {"type": "string", "enum": list(catalog.PLACEMENTS),
                      "description": "ugc: where the product is. table (default): beside the presenter, hands off. "
                                     "holding: the presenter holds it up."},
        "audio": {"type": "boolean", "description": "product_video: add sound."},
        "only": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 40},
                 "description": "Make only these line keys from the quote (default: all)."},
    }
    req = ["ad_type", "product_id", "lines"]
    if generating:
        props["confirm_total_credits"] = {"type": "integer", "minimum": 0,
                                          "description": "The total_credits quote_ad_batch returned for these exact "
                                                         "arguments, after the user agreed to it. A different total is "
                                                         "refused and nothing is made."}
        props["confirm_many_videos"] = {"type": "boolean", "description": "Needed above 8 videos in one batch."}
        req.append("confirm_total_credits")
    return _obj(props, req)


# ---------------------------------------------------------------- AI writing, planner, competitors

COPY_PROPS = {
    "kind": {"type": "string", "enum": list(platforms.KINDS), "description": "ad, hooks, captions, headlines, email or "
                                                                            "landing."},
    "platform": {"type": "string", "enum": list(platforms.PLATFORMS)},
    "variants": {"type": "integer", "enum": [3, 5]},
    "offer": {"type": "string", "maxLength": 300, "description": "What you're promoting."},
    "audience": {"type": "string", "maxLength": 200},
    "goal": {"type": "string", "enum": list(copywriter.GOALS)},
    "tone": {"type": "string", "maxLength": 40},
    "link": {"type": "string", "maxLength": 300},
    "notes": {"type": "string", "maxLength": 1000},
    "email_type": {"type": "string", "enum": list(platforms.EMAIL_TYPES)},
    "product_id": {"type": "integer", "minimum": 1, "description": "Default: the active product."},
}


def write_copy(ctx, args):
    with ctx.as_user():
        _need("copy")
    form = {"kind": args["kind"], "platform": args["platform"], "n": str(args.get("variants") or 3)}
    for k in ("offer", "audience", "goal", "tone", "link", "notes", "email_type"):
        if args.get(k):
            form[k] = args[k]
    if args.get("product_id"):
        form["product"] = str(args["product_id"])
    r = ctx.call("POST", "/app/copy", form=form)
    rid = _created_id(r, r"/app/copy/(\d+)")
    with ctx.as_user() as conn:
        row = conn.execute("SELECT * FROM copy_runs WHERE id=? AND workspace_id=?", (rid, ctx.account_id)).fetchone()
    return {"copy_run_id": rid, "kind": row["kind"], "platform": row["platform"], "credits_charged": row["credits"],
            "variants": json.loads(row["output_json"] or "{}").get("variants") or [], "credits_left": _balance_of(ctx)}


def _balance_of(ctx):
    with ctx.as_user():
        return _balance()


def start_plan(ctx, args):
    with ctx.as_user():
        _need("planner")
        cap = planner.cap_credits()
    if args["confirm_max_credits"] < cap:
        raise ToolError(f"A plan can use up to {cap} credits. Confirm that with the user and send "
                        f"confirm_max_credits={cap}.")
    form = {k: args[k] for k in ("offer", "audience", "goal", "budget", "dates", "tone", "link", "notes") if args.get(k)}
    form["platforms"] = args.get("platforms") or []
    if args.get("product_id"):
        form["product"] = str(args["product_id"])
    r = ctx.call("POST", "/app/campaigns", form=form)
    cid = _created_id(r, r"/app/campaigns/(\d+)")
    return {"campaign_id": cid, "status": "planning", "max_credits": cap,
            "note": "Planning takes a few minutes. Check it with get_campaign_plan.", "url": f"{ctx.base_url}/app/campaigns/{cid}"}


def get_plan(ctx, args):
    cid = args["campaign_id"]
    st = ctx.must("GET", f"/app/campaigns/{cid}/status").data          # plan feature + ownership
    with ctx.as_user() as conn:
        c = conn.execute("SELECT * FROM campaigns WHERE id=? AND workspace_id=?", (cid, ctx.account_id)).fetchone()
        ads = conn.execute("SELECT id, angle, platform, tool, template_id, brief, credits, status, job_id, params_json "
                           "FROM campaign_ads "
                           "WHERE campaign_id=? AND workspace_id=? ORDER BY id", (cid, ctx.account_id)).fetchall()
    return {"campaign_id": cid, "name": c["name"], "status": st["status"], "credits_used": c["credits"],
            "error": c["error"], "brief": json.loads(c["brief_json"] or "{}"), "plan": json.loads(c["plan_json"] or "{}"),
            "proposed_ads": [{**{k: a[k] for k in a.keys() if k != "params_json"},
                              "type": catalog.job_ad_type(a["tool"], a["params_json"])} for a in ads],
            "note": "Proposed ads are made from the campaign page in the app, or with quote_ad_batch and the generate tools."}


def list_plans(ctx, args):
    with ctx.as_user() as conn:
        _need("planner")
        rows = conn.execute("SELECT id, name, status, credits, created_at FROM campaigns WHERE workspace_id=? ORDER BY id DESC "
                            "LIMIT 100", (ctx.account_id,)).fetchall()
    return paginate([dict(r, campaign_id=r["id"]) for r in rows], args, key="campaigns")


def start_snapshot(ctx, args):
    with ctx.as_user():
        _need("competitors")
    comps = args.get("competitors") or []
    need = competitors.quote(len(competitors.parse_inputs(comps)))
    if args["confirm_max_credits"] < need:
        raise ToolError(f"This run can use up to {need} credits. Confirm that with the user and send "
                        f"confirm_max_credits={need}.")
    r = ctx.call("POST", "/app/competitors", form={"competitor": comps, "quote": str(args["confirm_max_credits"])})
    rid = _created_id(r, r"/app/competitors/(\d+)")
    return {"snapshot_id": rid, "status": "running", "max_credits": need,
            "note": "Research takes a few minutes. Check it with get_competitor_snapshot.",
            "url": f"{ctx.base_url}/app/competitors/{rid}"}


def get_snapshot(ctx, args):
    rid = args["snapshot_id"]
    st = ctx.must("GET", f"/app/competitors/{rid}/status").data
    out = {"snapshot_id": rid, "status": st["status"]}
    if st["status"] == "ready":
        md = ctx.must("GET", f"/app/competitors/{rid}.md")
        out["report_markdown"] = md.text
    elif st["status"] == "failed":
        with ctx.as_user() as conn:
            r = conn.execute("SELECT error FROM competitor_reports WHERE id=? AND workspace_id=?",
                             (rid, ctx.account_id)).fetchone()
        out["error"] = r["error"] if r else None
    return out


def list_snapshots(ctx, args):
    with ctx.as_user() as conn:
        _need("competitors")
        rows = conn.execute("SELECT id, status, credits, created_at, inputs_json, report_json FROM competitor_reports "
                            "WHERE workspace_id=? ORDER BY id DESC LIMIT 50", (ctx.account_id,)).fetchall()
    items = [{"snapshot_id": r["id"], "status": r["status"], "credits": r["credits"], "created_at": r["created_at"],
              "competitors": snapshot_competitors(r["inputs_json"], r["report_json"])} for r in rows]
    return paginate(items, args, key="snapshots")


def snapshot_competitors(inputs_json, report_json) -> list:
    """Who a snapshot is about: the names the member typed, else (a "find them for me" run starts with none) the ones
    the research found, which are only in the report. A row with no name shows its website."""
    def load(raw):
        try:
            d = json.loads(raw or "{}")
        except (ValueError, TypeError):
            d = {}
        return [c for c in (d.get("competitors") if isinstance(d, dict) else None) or [] if isinstance(c, dict)]
    rows = load(inputs_json) or load(report_json)
    return [n for n in (c.get("name") or c.get("website") or c.get("url") for c in rows) if n]


# ---------------------------------------------------------------- the library's own assets (site admins only)

def _admin_only(ctx):
    if not _site_admin(ctx):
        raise ToolError("This tool is for Campaignify's own admins.")


def _lib(ctx, args, fn, *extra):
    _admin_only(ctx)
    with ctx.as_user() as conn:
        try:
            return fn(conn, g.ws, args["kind"], args["ids"], args.get("options") or {}, *extra)
        except library.LibraryError as e:
            raise ToolError(str(e)) from None


def library_quote(ctx, args):
    q = _lib(ctx, args, library.quote)
    q["next"] = ("To make these, call generate_library_assets with the same kind, ids and options and "
                 f"confirm_total_credits={q['total_credits']}. Ask the user first.")
    return q


def library_generate(ctx, args):
    r = _lib(ctx, args, library.generate, args["confirm_total_credits"])
    r["note"] = "Queued. Stills take about a minute, clips a few minutes. list_library_assets shows them."
    r["url"] = f"{ctx.base_url}/admin/library"
    return r


def library_list(ctx, args):
    _admin_only(ctx)
    with ctx.as_user() as conn:
        rows = library.listing(conn, ctx.account_id, args.get("kind"))
    res = paginate(rows, args, default=50, most=100, key="assets")
    for a in res["assets"]:
        for o in a["outputs"]:
            o["download_url"] = ctx.file_link("output", o["output_id"])["url"]
    res["note"] = ("Every file has an `output_id` (get_asset_download_url takes it) and a `filename`: the name the "
                   "export zip gives it. `in_export` marks the asset the zip takes for its tag (the approved one, else "
                   "the newest). Save the downloads under those names in one folder and run "
                   "tools/import_library_assets.py <folder> to put them under static/. Links last 1 hour.")
    return res


LIB_OPTIONS = {"type": "object", "additionalProperties": False, "properties": {
    "quality": {"type": "string", "enum": ["low", "medium", "high"], "description": "Stills: default high."},
    "resolution": {"type": "string", "enum": ["1k", "2k"], "description": "Stills: default 1k."},
    "demo": {"type": "string", "enum": list(library.DEMO_IDS), "description": "Preset examples: which demo product."},
    "presenter": {"type": "string", "enum": list(presenters.IDS), "description": "Video examples: who presents."},
    "situation": {"type": "string", "enum": list(presenters.SITUATIONS)},
    "voice": {"type": "string", "enum": list(catalog.VOICE_BY_ID)},
    "anchor_only": {"type": "boolean", "description": "Presenter stills: only each presenter's first still."},
    "only_missing": {"type": "boolean", "description": "Preset examples: skip presets that already have one."},
    "video_seconds": {"type": "integer", "minimum": 1, "maximum": 120,
                      "description": "preset_videos: the clip's length in seconds (default: the preset's own). Kept "
                                     "inside what can be made: 4 to 30 for presenter and voiceover presets, 5 or 10 for "
                                     "product-video presets. The quote's lines show the length used."},
    "video_resolution": {"type": "string", "enum": list(library.VIDEO_RESOLUTIONS),
                         "description": "preset_videos: default 720p. 480p is the cheapest and for library examples "
                                        "only."},
    "voice_seconds": {"type": "integer", "minimum": library.VOICE_SECONDS_RANGE[0],
                      "maximum": library.VOICE_SECONDS_RANGE[1],
                      "description": f"presenter_voices: the sample's length (default {library.VOICE_SECONDS})."}}}
LIB_PROPS = {
    "kind": {"type": "string", "enum": list(library.KINDS),
             "description": "demo_products (packshots of the demo brand), presenter_stills, presenter_voices, "
                            "preset_examples (images) or preset_videos."},
    "ids": {"type": "array", "minItems": 1, "maxItems": 150, "items": {"type": "string", "maxLength": 80},
            "description": "presenter_stills: \"maya\" (all her situations) or \"maya:kitchen\"; presenter_voices: "
                           "presenter ids; preset_*: preset ids; demo_products: mug, candle, skincare, coffee, tote, "
                           "greens, app. [\"all\"] for everything of that kind."},
    "options": LIB_OPTIONS}

PRODUCT = {"type": "integer", "minimum": 1}

TOOLS = [
    Tool("get_account_status", "Plan and credits",
         "This workspace's plan, credit balance, which tools the plan includes, and whether the member's email is "
         "confirmed (videos need it). Credit packs and plans are bought in the app.", _obj({}), account_status),
    Tool("list_brands_products", "List brands and products",
         "Brands and products in the workspace with how many photos each has.", _obj(dict(PAGE_PROPS)), list_products),
    Tool("get_product", "Product details",
         "One product: price, approved claims, audience, photo ids, saved angles and scripts, and the image templates "
         "that fit it.", _obj({"product_id": PRODUCT}, ["product_id"]), get_product),
    Tool("list_ad_options", "Ad types, presets, presenters, voices",
         "What Create can make: ad types, shapes, lengths, the preset library (each preset with its tags, price and "
         "example), the AI presenter roster with each presenter's situations and voice, the voices, and copy kinds.",
         _obj({}), options),
    Tool("quote_ai_tasks", "Credit estimates for AI writing",
         "The most ad copy, a campaign plan and a competitor snapshot can use, in credits. Nothing is charged.",
         _obj({}), quote_ai),
    Tool("quote_ad_batch", "Price an ad batch",
         "Prices a batch of image ads or videos before anything is made: total credits, each ad's line, and what "
         "the workspace has. Nothing is made or charged. Show the total to the user, then pass it as "
         "confirm_total_credits to generate_image_ads or generate_video_ads.",
         _ad_schema(SPOKEN + IMAGE + SCENE, False), quote_batch),
    Tool("generate_image_ads", "Make image ads (spends credits)",
         "SPENDS CREDITS. Makes the image ads quote_ad_batch priced. Needs confirm_total_credits equal to that quote; "
         "if the price changed, nothing is made. Ask the user before calling.",
         _ad_schema(IMAGE, True), lambda ctx, a: generate(ctx, a, IMAGE), scope="write"),
    Tool("generate_video_ads", "Make AI presenter or product videos (spends credits)",
         "SPENDS CREDITS. Makes AI presenter (UGC) videos, voiceover product videos or short product videos that "
         "quote_ad_batch priced. Needs confirm_total_credits equal to that quote, and confirm_many_videos above 8 "
         "videos; otherwise nothing is made. Presenters are AI-generated people, never real ones. Ask the user first.",
         _ad_schema(SPOKEN + SCENE, True), lambda ctx, a: generate(ctx, a, SPOKEN + SCENE), scope="write"),
    Tool("get_batch", "Batch progress",
         "A batch made by a generate tool: how many ads are ready, each one's status and output ids.",
         _obj({"batch_id": {"type": "integer", "minimum": 1}}, ["batch_id"]), get_batch),
    Tool("list_assets", "List ads made",
         "Everything made in the workspace, newest first: type, status, label, hook, product and output ids.",
         _obj(dict(PAGE_PROPS, product_id=PRODUCT, batch_id={"type": "integer", "minimum": 1},
                   type={"type": "string", "enum": list(ASSET_TYPES)},
                   status={"type": "string", "enum": ["done", "making", "failed"]})), list_assets),
    Tool("get_asset_download_url", "Asset download link",
         "A private download link for one made image or video (output_id from list_assets), valid for 1 hour.",
         _obj({"output_id": {"type": "integer", "minimum": 1}}, ["output_id"]), download_link),
    Tool("generate_ad_copy", "Write ad copy (spends a few credits)",
         "Writes 3 or 5 variants of ad copy, hooks, captions, headlines, an email or a landing page for a platform, "
         "from the product's brief. Charged at what the writing uses (quote_ai_tasks shows the most). Starter plan "
         "and up.", _obj(COPY_PROPS, ["kind", "platform"]), write_copy, scope="write"),
    Tool("start_campaign_plan", "Plan a campaign (spends credits)",
         "SPENDS CREDITS. Starts the AI campaign planner: angles, platforms and proposed ads from your offer. Charged "
         "at what it uses, up to the cap in quote_ai_tasks; confirm_max_credits must be that cap. Starter plan and up. "
         "It plans only: it does not post or run ads.",
         _obj({"offer": {"type": "string", "minLength": 1, "maxLength": 300},
               "audience": {"type": "string", "maxLength": 200}, "goal": {"type": "string", "maxLength": 20},
               "budget": {"type": "string", "maxLength": 80}, "dates": {"type": "string", "maxLength": 120},
               "tone": {"type": "string", "maxLength": 40}, "link": {"type": "string", "maxLength": 300},
               "notes": {"type": "string", "maxLength": 1500},
               "platforms": {"type": "array", "maxItems": 6, "items": {"type": "string", "enum": list(platforms.PLATFORMS)}},
               "product_id": PRODUCT,
               "confirm_max_credits": {"type": "integer", "minimum": 0,
                                       "description": "The planner's cap from quote_ai_tasks, after the user agreed."}},
              ["offer", "confirm_max_credits"]), start_plan, scope="write"),
    Tool("get_campaign_plan", "Campaign plan",
         "A campaign plan's status, the plan itself and its proposed ads.",
         _obj({"campaign_id": {"type": "integer", "minimum": 1}}, ["campaign_id"]), get_plan),
    Tool("list_campaign_plans", "List campaign plans", "Campaign plans in the workspace, newest first.",
         _obj(dict(PAGE_PROPS)), list_plans),
    Tool("start_competitor_snapshot", "Research competitors (spends credits)",
         "SPENDS CREDITS. Researches up to 5 competitors (names or sites; leave empty to have it find them) and writes "
         "a sourced snapshot of their offers, pricing and messaging. Charged at what it uses, up to the amount in "
         "quote_ai_tasks; confirm_max_credits must be at least that.",
         _obj({"competitors": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 200}},
               "confirm_max_credits": {"type": "integer", "minimum": 0}}, ["confirm_max_credits"]),
         start_snapshot, scope="write", open_world=True),
    Tool("get_competitor_snapshot", "Competitor snapshot",
         "A snapshot's status and, when ready, the full report as Markdown with its sources.",
         _obj({"snapshot_id": {"type": "integer", "minimum": 1}}, ["snapshot_id"]), get_snapshot),
    Tool("list_competitor_snapshots", "List competitor snapshots", "Competitor snapshots in the workspace, newest first.",
         _obj(dict(PAGE_PROPS)), list_snapshots),
    # ---- Campaignify's own admins only: hidden from everyone else in tools/list and refused when called
    Tool("quote_library_assets", "Price library assets (admin)",
         "Admin only. Prices a run of Campaignify's own library assets (presenter stills and voice samples, demo "
         "product packshots, preset examples) before anything is made: total credits, our estimate of the vendor's "
         "cost in USD, and each line. Nothing is made or charged.",
         _obj(LIB_PROPS, ["kind", "ids"]), library_quote, visible=_site_admin),
    Tool("generate_library_assets", "Make library assets (admin, spends credits)",
         "Admin only. SPENDS CREDITS. Makes the library assets quote_library_assets priced, as ordinary jobs in this "
         "workspace. Needs confirm_total_credits equal to that quote; otherwise nothing is made. Ask the user first.",
         _obj(dict(LIB_PROPS, confirm_total_credits={"type": "integer", "minimum": 0,
                                                     "description": "The total_credits quote_library_assets returned "
                                                                    "for these exact arguments."}),
              ["kind", "ids", "confirm_total_credits"]), library_generate, scope="write", visible=_site_admin),
    Tool("list_library_assets", "List library assets (admin)",
         "Admin only. The library assets made in this workspace, newest first: asset ids, the presenter, situation "
         "or preset each belongs to, status, and per file its output_id (for get_asset_download_url), its export "
         "filename and a private download link.",
         _obj(dict(PAGE_PROPS, kind={"type": "string", "enum": list(library.KINDS)})), library_list,
         visible=_site_admin),
]


class CampaignifyAdapter(mcpserver.Adapter):
    name = "campaignify"
    title = "Campaignify"
    version = "1.0"
    icons = [("/static/img/campaignify-icon-512.png", "image/png", "512x512"),
             ("/static/img/campaignify-icon-192.png", "image/png", "192x192"), ("/favicon.ico", "image/x-icon", "48x48")]

    def web_page(self):
        """GET /mcp in a browser: the Campaignify MCP page (app.mcp_page)."""
        return current_app.view_functions["mcp_page"]()
    token_prefix = "cfy"
    settings_path = "/app/mcp"
    docs_path = "/docs/mcp"
    instructions = ("Campaignify makes ad creative from a product: image ads, AI presenter (UGC) videos, product videos, "
                    "ad copy, campaign plans and competitor snapshots. Costs are in credits. Before making images or "
                    "videos, call quote_ad_batch and show the total to the user; only call a generate tool after they "
                    "agree, with confirm_total_credits set to that total. Campaignify makes the creative; it does not "
                    "post or run ads.")
    tools = TOOLS

    def connect(self):
        return db.connect()

    def web_user(self):
        return {"id": g.user["id"], "email": g.user["email"]} if g.get("user") else None

    def web_account(self):
        if not g.get("ws") or g.get("viewing"):
            return None          # an admin viewing someone else's workspace can't make keys for it
        return {"id": g.ws["id"], "name": g.ws["name"], "role": g.user["role"]}

    def workspaces(self, conn, user_id):
        r = conn.execute("SELECT w.id, w.name, u.role FROM users u JOIN workspaces w ON w.id=u.workspace_id WHERE u.id=?",
                         (user_id,)).fetchone()
        return [{"id": r["id"], "name": r["name"], "role": r["role"]}] if r else []

    def member_role(self, conn, user_id, account_id):
        r = conn.execute("SELECT role FROM users WHERE id=? AND workspace_id=?", (user_id, account_id)).fetchone()
        return r["role"] if r else None

    def enter(self, conn, user_id, account_id):
        """What app.load_user sets for a logged-in member, from the token.
        Never admin powers (is_admin off) and never an admin's "view"."""
        u = conn.execute("SELECT * FROM users WHERE id=? AND workspace_id=?", (user_id, account_id)).fetchone()
        if not u:
            raise ToolError("This key's user is no longer in the workspace.")
        g.user = dict(u, is_admin=0)
        g.viewing = None
        core.load_workspace(conn, account_id)
        g.mcp_token = True

    def csrf_value(self):
        return core.csrf_token()

    def csrf_ok(self):
        import secrets
        from flask import request, session
        sent = request.form.get("csrf") or ""
        return bool(sent) and secrets.compare_digest(sent, session.get("csrf", ""))

    def serve_file(self, kind, obj_id):
        if kind != "output":
            from flask import abort
            abort(404)
        return current_app.view_functions["media"](oid=obj_id)

    def refusal(self, r):
        if r.status == 402 and not r.data:
            return "That needs a higher plan or more credits. See plans and credit packs on the Billing page."
        if r.status == 404:
            return "Not found in this workspace."
        msg = r.message()
        if isinstance(r.data, dict) and r.data.get("verify"):
            msg += " (Confirm the email address in the app first.)"
        return msg


def init(app):
    mcpserver.init(app, CampaignifyAdapter())

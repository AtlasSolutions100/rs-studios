"""The library's own assets, made through Campaignify's own pipeline by a site admin (m15).

What gets made, as ordinary jobs in the admin's own workspace (so they show in its Library, can be approved,
downloaded and exported like any ad):

    demo_products      a packshot for each product of the fictional demo brand (DEMO), text-to-image
    presenter_stills   a presenter in a situation (presenters.py). The first situation of a presenter is plain
                       text-to-image; every other one is "the same person, somewhere new": the first still goes in
                       as the reference image and only the place and the clothes change
    presenter_voices   six seconds of a presenter talking, from their still: the import script keeps its sound as
                       the presenter's voice sample (voice.mp3)
    preset_examples    an image preset run exactly as a member would run it, on a demo product
    preset_videos      a video preset (one that runs today), the same way

Price first, like everything else here: quote() says what a run costs in credits and what we estimate the vendor
charges in dollars; generate() starts nothing unless the total it is given is that quote. Every job carries
params.library = {kind, presenter, situation, preset, demo}, and assets() / export_zip() name each file by those
tags (presenter__maya__kitchen.png, preset__offer.png, voice__maya.mp4 ...), which is what
tools/import_library_assets.py reads to put the approved ones under static/.

Admin only: the routes (create_routes: /admin/library) are behind admin_required, the MCP tools are hidden from and
refused to everyone else (mcp_tools). Nothing here runs by itself: an admin asks for a quote, then confirms it.
"""
import io
import json
import os
import shutil
import time
import uuid
import zipfile

from flask import g

import catalog
import db
import hf
import presenters
import presets

KINDS = ("demo_products", "presenter_stills", "presenter_voices", "preset_examples", "preset_videos")
TAG = {"demo_products": "demo_product", "presenter_stills": "presenter_still", "presenter_voices": "presenter_voice",
       "preset_examples": "preset_example", "preset_videos": "preset_video"}
MAX_JOBS = 120                 # one generate call
VOICE_SECONDS = 6
VOICE_RESOLUTION = "480p"
ESTIMATE_TTL = 900             # a live vendor estimate is reused for 15 minutes, so a quote and its generate agree


class LibraryError(ValueError):
    """Why a library run can't be priced or started; the message is for the admin."""


# ---------------------------------------------------------------- the demo brand
# Fictional, ours, and plain: one house brand with a product per category, so every preset has something to be
# shown on. The words on each label are in its packshot prompt and nowhere claim anything. (No trademark search
# was done on the name; change DEMO_BRAND and the prompts together if it ever collides.)
DEMO_BRAND = "Dapplewood"
_PACKSHOT = ("Studio packshot for an online shop: {what} Pure white seamless background, a soft natural shadow under "
             "it, seen front-on at product height, the whole product in frame with room around it, the label sharp, "
             "flat and fully readable. The only text anywhere is the label described. No props, no hands, no people, "
             "no extra logos, no watermark.")
DEMO = {
    "mug": {"name": "Dapplewood Trail Mug", "category": "home", "tags": "insulated mug, travel mug", "price": "$24",
            "offer": "Free lid with every mug", "claims": "Double-wall stainless steel\n12 oz\nPress-fit lid",
            "description": "A 12 oz insulated steel mug with a press-fit lid.", "aspect": "3:4",
            "what": "a matte forest-green insulated steel mug with a black press-fit lid. On its side is a small cream "
                    "label printed with the word DAPPLEWOOD in clean capital letters and, under it in smaller type, "
                    "Trail Mug 12 oz."},
    "candle": {"name": "Dapplewood Cedar Candle", "category": "home", "tags": "candle, home", "price": "$18",
               "offer": "", "claims": "Soy wax\n8 oz\nCotton wick",
               "description": "An 8 oz soy candle in an amber glass jar.", "aspect": "3:4",
               "what": "a soy candle in an amber glass jar with a brushed metal lid resting beside it. On the jar is a "
                       "cream paper label printed with the word DAPPLEWOOD in clean capital letters and, under it in "
                       "smaller type, Cedar Candle 8 oz."},
    "skincare": {"name": "Dapplewood Daily Moisturizer", "category": "beauty", "tags": "skincare, moisturizer",
                 "price": "$22", "offer": "", "claims": "Fragrance free\n50 ml",
                 "description": "A 50 ml fragrance-free daily moisturizer in a pump bottle.", "aspect": "3:4",
                 "what": "a frosted white skincare pump bottle with a matte white pump. On its front is a minimal label "
                         "printed with the word DAPPLEWOOD in clean grey capital letters and, under it in smaller type, "
                         "Daily Moisturizer 50 ml."},
    "coffee": {"name": "Dapplewood Morning Roast", "category": "food_drink", "tags": "instant coffee, coffee",
               "price": "$16", "offer": "", "claims": "Instant coffee\n30 cups\nMedium roast",
               "description": "Medium-roast instant coffee, 30 cups to a bag.", "aspect": "3:4",
               "what": "a kraft-paper stand-up coffee bag with a resealable top. On its front is a dark green label "
                       "printed with the word DAPPLEWOOD in clean cream capital letters and, under it in smaller type, "
                       "Morning Roast Instant Coffee 30 cups."},
    "tote": {"name": "Dapplewood Canvas Tote", "category": "apparel", "tags": "bag, tote", "price": "$28",
             "offer": "", "claims": "Heavy cotton canvas\nInside pocket",
             "description": "A heavy cotton canvas tote with an inside pocket.", "aspect": "3:4",
             "what": "a natural heavy-canvas tote bag with long handles, standing upright and softly filled. Sewn on "
                     "its front is a small dark green woven label with the word DAPPLEWOOD in clean cream capital "
                     "letters."},
    "greens": {"name": "Dapplewood Daily Greens", "category": "supplement", "tags": "supplement, greens powder",
               "price": "$34", "offer": "", "claims": "30 servings\nUnflavoured",
               "description": "An unflavoured greens powder, 30 servings to a tub.", "aspect": "3:4",
               "what": "a matte sage-green supplement tub with a white screw lid. On its front is a white label "
                       "printed with the word DAPPLEWOOD in clean dark green capital letters and, under it in smaller "
                       "type, Daily Greens 30 servings."},
    "app": {"name": "Dapplewood Planner", "category": "app_service", "tags": "app, planner", "price": "Free to start",
            "offer": "", "claims": "Week view\nShared lists",
            "description": "A simple weekly planner app with shared lists.", "aspect": "9:16", "screen": True,
            "what": ""},
}
_APP_SCREEN = ("A flat, clean screenshot of a phone app in portrait, edge to edge, with no device frame, no hands and no "
               "background: a weekly planner. At the top the app's name, Dapplewood Planner, in dark green. Under it a "
               "row of seven day chips with one selected, then three task rows with round checkboxes reading Water the "
               "plants, Call the plumber and Plan the weekend, and at the bottom one rounded dark green button reading "
               "Add a task. White background, generous spacing, one plain sans-serif typeface. No other text, no "
               "logos, no watermark.")
DEMO_IDS = tuple(DEMO)
_DEMO_FOR_CATEGORY = {"food_drink": "coffee", "beauty": "skincare", "apparel": "tote", "supplement": "greens",
                      "home": "candle", "app_service": "app", "other": "mug"}
_ROTATION = ("mug", "candle", "skincare", "tote", "mug", "coffee")


def packshot_prompt(demo_id: str) -> str:
    d = DEMO[demo_id]
    return _APP_SCREEN if d.get("screen") else _PACKSHOT.format(what=d["what"])


# The words of each example (only what the demo product's saved details back; the review card shows a placeholder
# that says it is one, never a made-up review).
_WORDS = {
    "clean-headline": {"headline": "Made for the trail", "subline": "The Dapplewood Trail Mug", "cta": "Shop now"},
    "offer": {"headline": "The mug that goes with you", "cta": "Get yours", "layers": {"badge": "$24"}},
    "callouts": {"headline": "What you get", "cta": "See more",
                 "layers": {"callouts": ["Double-wall steel", "12 oz", "Press-fit lid"]}},
    "review": {"headline": "Your customer's real review goes here, word for word. We never write one.",
               "layers": {"reviewer": "Their name", "stars": 0, "attest": True}},
    "compare": {"headline": "One mug, every day",
                "layers": {"left_title": "A paper cup", "right_title": "Trail Mug",
                           "left": ["Used once", "No lid that stays on", "Another one tomorrow"],
                           "right": ["Yours to keep", "Press-fit lid", "Steel, 12 oz"]}},
    "marketplace": {},
    "season-winter-gift": {"headline": "For the one who is always out early", "cta": "Shop gifts"},
    "season-fresh-start": {"headline": "A calmer start", "cta": "Shop now"},
    "season-spring": {"headline": "Open the windows", "cta": "Shop now"},
    "season-summer": {"headline": "Take it outside", "cta": "Shop now"},
    "season-autumn": {"headline": "Made for slow afternoons", "cta": "Shop now"},
    "season-back-to-routine": {"headline": "Back in the bag", "cta": "Shop now"},
    "season-gift-for-them": {"headline": "For them", "cta": "Shop gifts"},
    "season-offer": {"headline": "A free lid, this season", "cta": "Get yours", "layers": {"badge": "Free lid"}},
    "problem-answer": {"headline": "Lid gone missing again?", "subline": "This one presses in and stays.",
                       "cta": "See the mug"},
    "three-reasons": {"headline": "Three reasons", "cta": "See more",
                      "layers": {"callouts": ["Double-wall steel", "Press-fit lid", "12 oz, fits a bag"]}},
    "how-it-works": {"headline": "How it works", "cta": "See more",
                     "layers": {"callouts": ["Fill it", "Press the lid in", "Take it with you"]}},
    "then-now": {"headline": "One mug, not a stack of cups",
                 "layers": {"left_title": "The old way", "right_title": "With Trail Mug",
                            "left": ["A paper cup a day", "Lids in every drawer", "Nothing to carry it in"],
                            "right": ["One steel mug", "One press-fit lid", "12 oz, fits a bag"], "true": True}},
    "app-screen": {"headline": "Your week, on one screen", "cta": "Try it free"},
}
_DEMO_OF = {"season-autumn": "candle", "season-winter-gift": "mug", "season-spring": "skincare",
            "season-gift-for-them": "candle", "season-back-to-routine": "tote", "season-fresh-start": "greens",
            "season-summer": "tote", "app-screen": "app"}
# What each video example says (within its length; the presenter explains, never "I bought it").
_SCRIPTS = {
    "vo-product-shots": ("mug", "Meet the Dapplewood Trail Mug. Double-wall stainless steel, twelve ounces, with a lid "
                                "that presses in and stays. Find it at Dapplewood."),
    "hands-only-demo": ("mug", "This is the Dapplewood Trail Mug. Fill it, press the lid in, and take it with you. "
                               "Find it at Dapplewood."),
    "pov-unboxing": ("candle", "This is what arrives when you order the Dapplewood Cedar Candle. Soy wax, a cotton "
                               "wick, eight ounces in amber glass. Find it at Dapplewood."),
    "talk-to-camera": ("mug", "Quick one about the Dapplewood Trail Mug. Steel, twelve ounces, and the lid presses in."),
    "car-talk": ("mug", "Okay, quick thing about the Dapplewood Trail Mug. Twelve ounces, steel, and a lid that stays."),
    "season-gift-clip": ("candle", "A present they will actually use. The Dapplewood Cedar Candle, at Dapplewood."),
    "founder-story": ("candle", "We started making Dapplewood candles because we wanted one plain, well-made thing on "
                                "the table. We pour soy wax around a cotton wick, eight ounces at a time. Come and see "
                                "what we make."),
    "turn": ("mug", ""), "swap": ("coffee", ""), "label": ("coffee", ""), "how": ("coffee", ""),
}
VOICE_DEMO = "mug"


def demo_for(p) -> str:
    """Which demo product a preset's example is shown on."""
    if p.id in _DEMO_OF:
        return _DEMO_OF[p.id]
    if p.id in _SCRIPTS:
        return _SCRIPTS[p.id][0]
    if p.fits == "coffee":
        return "coffee"
    if isinstance(p.fits, tuple):
        return _DEMO_FOR_CATEGORY.get(p.fits[0], "mug")
    if p.layout:
        return "mug"
    plain = [x.id for x in presets.OLD_TEMPLATES + presets.SCENES if x.fits == "any" and x.tool == "ad_image"]
    return _ROTATION[plain.index(p.id) % len(_ROTATION)] if p.id in plain else "mug"


# ---------------------------------------------------------------- reading what was made

def _params(job) -> dict:
    try:
        d = json.loads(job["params_json"] or "{}")
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def tag_of(job) -> dict | None:
    lib = _params(job).get("library")
    return lib if isinstance(lib, dict) and lib.get("kind") in TAG.values() else None


def filename(tag: dict, ext: str = "") -> str:
    """The export name of an asset from its tags (what tools/import_library_assets.py reads)."""
    k = tag.get("kind")
    if k == "presenter_still":
        base = f"presenter__{tag.get('presenter')}__{tag.get('situation')}"
    elif k == "presenter_voice":
        base = f"voice__{tag.get('presenter')}"
    elif k == "demo_product":
        base = f"demo__{tag.get('demo')}"
    else:
        base = f"preset__{tag.get('preset')}"
    return base + ext


def assets(conn, ws_id: int, kind: str | None = None, limit: int = 600) -> list[dict]:
    """Every library job of this workspace, newest first: its tags, status, outputs and export name. `kind` is one of
    KINDS (or a tag kind)."""
    want = TAG.get(kind, kind) if kind else None
    rows = conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND params_json LIKE ? "
                        "ORDER BY id DESC LIMIT ?", (ws_id, '%"library"%', limit)).fetchall()
    ids = [r["id"] for r in rows]
    outs, verdicts = {}, {}
    if ids:
        marks = ",".join("?" * len(ids))
        for o in conn.execute(f"SELECT id, job_id, kind, path FROM outputs WHERE job_id IN ({marks}) ORDER BY id", ids):
            outs.setdefault(o["job_id"], []).append(o)
        for v in conn.execute(f"SELECT job_id, verdict FROM job_reviews WHERE job_id IN ({marks}) ORDER BY at", ids):
            verdicts[v["job_id"]] = v["verdict"]
    out = []
    for r in rows:
        tag = tag_of(r)
        if tag is None or (want and tag["kind"] != want):
            continue
        files = []
        for o in outs.get(r["id"], []):
            ext = os.path.splitext(o["path"] or "")[1].lower() or (".mp4" if o["kind"] == "video" else ".png")
            files.append({"output_id": o["id"], "kind": o["kind"], "filename": filename(tag, ext)})
        out.append({"job_id": r["id"], "kind": tag["kind"], "presenter": tag.get("presenter"),
                    "situation": tag.get("situation"), "preset": tag.get("preset"), "demo": tag.get("demo"),
                    "status": r["status"], "credits": r["credits"], "usd": r["cost_usd"], "created_at": r["created_at"],
                    "error": r["error"] if r["status"] == "failed" else None, "verdict": verdicts.get(r["id"]),
                    "batch_id": r["batch_id"], "outputs": files})
    return out


def picks(conn, ws_id: int, kind: str | None = None) -> list[dict]:
    """One finished asset per tag, for the export: the newest approved one, else the newest that wasn't rejected."""
    best = {}
    for a in assets(conn, ws_id, kind):
        if a["status"] != "done" or not a["outputs"] or a["verdict"] in ("reject", "rejected"):
            continue
        key = (a["kind"], a["presenter"], a["situation"], a["preset"], a["demo"])
        have = best.get(key)
        if have is None or (a["verdict"] in ("approve", "approved") and have["verdict"] not in ("approve", "approved")):
            best[key] = a
    return list(best.values())


def export_zip(conn, ws_id: int, kind: str | None = None) -> bytes:
    """The picked assets as one zip, each file named by its tags, with a manifest.json beside them."""
    buf, manifest = io.BytesIO(), []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for a in picks(conn, ws_id, kind):
            o = a["outputs"][0]
            row = conn.execute("SELECT path FROM outputs WHERE id=?", (o["output_id"],)).fetchone()
            if not row or not row["path"] or not os.path.exists(row["path"]):
                continue
            z.write(row["path"], o["filename"])
            manifest.append({k: a[k] for k in ("job_id", "kind", "presenter", "situation", "preset", "demo")}
                            | {"file": o["filename"]})
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    return buf.getvalue()


def _latest_done(conn, ws_id: int, kind: str, **match) -> dict | None:
    for a in assets(conn, ws_id, kind):
        if a["status"] == "done" and a["outputs"] and a["verdict"] not in ("reject", "rejected") and \
                all(a.get(k) == v for k, v in match.items()):
            return a
    return None


# ---------------------------------------------------------------- the demo products as real products of the workspace

def demo_product(conn, ws, demo_id: str):
    """The demo product's row in this workspace, or None while it hasn't been set up."""
    return conn.execute("SELECT p.* FROM products p JOIN brands b ON b.id=p.brand_id WHERE p.workspace_id=? AND "
                        "p.archived=0 AND p.name=? AND b.brand_name=? ORDER BY p.id LIMIT 1",
                        (ws["id"], DEMO[demo_id]["name"], DEMO_BRAND)).fetchone()


def ensure_demo(conn, ws, demo_id: str):
    """The demo product as a product of this workspace with its packshot as its photo, made on first use from the
    newest finished packshot. Raises LibraryError when there is no packshot yet."""
    import products
    if demo_id not in DEMO:
        raise LibraryError(f"Unknown demo product {demo_id!r}. Use one of: {', '.join(DEMO_IDS)}.")
    d = DEMO[demo_id]
    row = demo_product(conn, ws, demo_id)
    if row is not None and products.photos_for(conn, ws["id"], row["id"]):
        return row
    shot = _latest_done(conn, ws["id"], "demo_product", demo=demo_id)
    if shot is None:
        raise LibraryError(f"{d['name']} has no packshot yet. Make it first: kind demo_products, ids [\"{demo_id}\"].")
    src = conn.execute("SELECT path FROM outputs WHERE id=?", (shot["outputs"][0]["output_id"],)).fetchone()
    if not src or not src["path"] or not os.path.exists(src["path"]):
        raise LibraryError(f"{d['name']}'s packshot file isn't on this server any more. Make it again.")
    try:
        if row is None:
            b = conn.execute("SELECT id FROM brands WHERE workspace_id=? AND brand_name=?", (ws["id"], DEMO_BRAND)).fetchone()
            bid = b["id"] if b else products.create_brand(
                conn, ws, DEMO_BRAND, about="A fictional brand used for Campaignify's own example ads.")
            pid = products.create_product(conn, ws, bid, {k: d[k] for k in ("name", "description", "price", "offer",
                                                                                "claims", "tags", "category")})
        else:
            pid = row["id"]
    except products.ProductError as e:
        raise LibraryError(f"The demo brand ({DEMO_BRAND}) couldn't be added to this workspace: {e} The library needs "
                           "room for one more brand here: give this workspace a plan with more brands on the Admin "
                           "page, or remove one.") from None
    import core
    os.makedirs(core.UPLOADS, exist_ok=True)
    ext = os.path.splitext(src["path"])[1].lower() or ".png"
    dst = os.path.join(core.UPLOADS, f"{uuid.uuid4().hex}{ext}")
    shutil.copyfile(src["path"], dst)
    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "image/png")
    products.add_photo(conn, ws["id"], pid, dst, f"{demo_id}-packshot{ext}", ctype)
    return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


# ---------------------------------------------------------------- what a run would make

def _still_settings(options: dict) -> tuple[str, str]:
    q = str(options.get("quality") or catalog.LIBRARY_STILL_DEFAULT[0])
    r = str(options.get("resolution") or catalog.LIBRARY_STILL_DEFAULT[1])
    if (q, r) not in catalog.LIBRARY_STILL_USD:
        raise LibraryError("quality is low, medium or high; resolution is 1k or 2k.")
    return q, r


def _still_usd(conn, q: str, r: str, refs: int, aspect: str) -> tuple[float, bool]:
    """(dollars for one still, whether the vendor's own estimate said so). The vendor's free estimate is asked once
    per setting and kept for ESTIMATE_TTL seconds; with none, our table (catalog.LIBRARY_STILL_USD)."""
    table = catalog.library_still_usd(q, r, refs)
    key = f"library_estimate:{catalog.model_for('library_still')}:{q}:{r}:{refs}"
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        have = json.loads(row["value"]) if row else None
    except (ValueError, TypeError):
        have = None
    if have and time.time() - float(have.get("at") or 0) < ESTIMATE_TTL:
        return (float(have["usd"]), True) if have.get("usd") is not None else (table, False)
    usd = None
    if hf.configured():
        sample = {"prompt": "A person in a kitchen", "resolution": r, "aspect_ratio": aspect, "quality": q,
                  "enhance_prompt": False}
        if refs:
            sample["image_urls"] = ["https://campaignify.ai/static/previews/ad_image.jpg"] * refs
        try:
            usd = hf.estimate(catalog.model_for("library_still"), sample)
        except Exception:  # noqa: BLE001 — an estimate never blocks a quote
            usd = None
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, json.dumps({"usd": round(usd, 4) if usd is not None else None, "at": time.time()})))
    conn.commit()
    return (round(usd, 4), True) if usd is not None else (table, False)


def _credits(usd: float) -> int:
    import math
    return int(math.ceil(round(usd * catalog.MARKUP * 100, 6)))


def _still_item(conn, key, label, prompt, tag, q, r, aspect, refs=0, **extra) -> dict:
    usd, live = _still_usd(conn, q, r, refs, aspect)
    return {"key": key, "label": label, "tool": "library_still", "brief": prompt, "usd": usd, "credits": _credits(usd),
            "live": live, "params": dict(extra.pop("params", {}), quality=q, resolution=r, aspect=aspect,
                                         library=tag, label=label), **extra}


def _ids(ids, known, what: str) -> list[str]:
    ids = [str(x).strip() for x in (ids or []) if str(x).strip()]
    if not ids:
        raise LibraryError(f"Say which {what}: ids (or [\"all\"]).")
    if ids == ["all"]:
        return list(known)
    return list(dict.fromkeys(ids))


def _demo_items(conn, ws, ids, options) -> list[dict]:
    q, r = _still_settings(options)
    out = []
    for did in _ids(ids, DEMO_IDS, "demo products"):
        if did not in DEMO:
            raise LibraryError(f"Unknown demo product {did!r}. Use: {', '.join(DEMO_IDS)}.")
        d = DEMO[did]
        out.append(_still_item(conn, did, d["name"], packshot_prompt(did), {"kind": "demo_product", "demo": did},
                               q, r, d["aspect"]))
    return out


def _identity(conn, ws, pid: str, anchor: str) -> dict | None:
    """Where the "same person" reference of a presenter comes from when its first still isn't part of this run: the
    approved file under static/, else the newest finished first still in this workspace. None when there is none."""
    path = presenters.still_path(pid, anchor)
    if path:
        return {"ref_static": os.path.relpath(path, os.path.join(presenters.HERE, "static"))}
    job = _latest_done(conn, ws["id"], "presenter_still", presenter=pid, situation=anchor)
    return {"ref_job": job["job_id"]} if job else None


def _still_items(conn, ws, ids, options) -> list[dict]:
    q, r = _still_settings(options)
    want = {}                                   # presenter -> [situation ids], in the order asked
    all_ids = [f"{p['id']}:{s['id']}" for p in presenters.all_presenters() for s in p["situations"]]
    for raw in _ids(ids, all_ids, "presenters (\"maya\", or \"maya:kitchen\")"):
        pid, _, sid = raw.partition(":")
        p = presenters.record(pid)
        if p is None:
            raise LibraryError(f"Unknown presenter {pid!r}.")
        mine = presenters.situation_ids(pid)
        if sid and sid != "portrait" and sid not in mine:
            raise LibraryError(f"{p['name']} has no situation {sid!r}. Theirs: {', '.join(mine)}.")
        for s in ([sid] if sid else mine):
            if s not in want.setdefault(pid, []):
                want[pid].append(s)
    out = []
    for pid, sids in want.items():
        p, anchor = presenters.record(pid), presenters.default_situation(pid)
        if options.get("anchor_only"):
            sids = [anchor]
        sids = sorted(sids, key=lambda s: s != anchor)          # the first still first: the others start from it
        other = None if anchor in sids else _identity(conn, ws, pid, anchor)
        if anchor not in sids and other is None:
            raise LibraryError(f"{p['name']}'s first still ({anchor}) has to exist before the others: add "
                               f"\"{pid}:{anchor}\" to this run, or make and approve it first.")
        for sid in sids:
            first = sid == anchor
            tag = {"kind": "presenter_still", "presenter": pid, "situation": sid}
            name = "portrait" if sid == "portrait" else presenters.SITUATIONS[sid]["label"]
            prompt = presenters.portrait_prompt(pid, True) if sid == "portrait" else \
                presenters.still_prompt(pid, sid, same_person=not first)
            item = _still_item(conn, f"{pid}:{sid}", f"{p['name']} · {name}", prompt, tag, q, r,
                               "3:4" if sid == "portrait" else "9:16", refs=0 if first else 1)
            if not first:
                if other is not None:
                    item["params"].update(other)
                else:
                    item["after"] = f"{pid}:{anchor}"       # generate() fills ref_job with that job's id
            out.append(item)
    return out


def _presenter_still_ref(conn, ws, pid: str, sid: str) -> dict:
    """The still a library video of this presenter starts from: the approved file, else the newest finished one."""
    path = presenters.still_path(pid, sid)
    if path:
        return {"ref_static": os.path.relpath(path, os.path.join(presenters.HERE, "static"))}
    job = _latest_done(conn, ws["id"], "presenter_still", presenter=pid, situation=sid)
    return {"ref_job": job["job_id"]} if job else {}


def _voice_items(conn, ws, ids, options) -> list[dict]:
    out = []
    usd = round(catalog.LIBRARY_USD_PER_SECOND_480P * VOICE_SECONDS, 4)
    for pid in _ids(ids, presenters.IDS, "presenters"):
        p = presenters.record(pid)
        if p is None:
            raise LibraryError(f"Unknown presenter {pid!r}.")
        sid = presenters.default_situation(pid)
        ref = _presenter_still_ref(conn, ws, pid, sid)
        if not ref:
            raise LibraryError(f"{p['name']} needs their first still ({sid}) before a voice sample: the clip starts "
                               "from it.")
        params = dict(ref, ad_type="ugc", presenter=pid, voice=p["voice"], situation=sid, duration=VOICE_SECONDS,
                      aspect="9:16", resolution=VOICE_RESOLUTION, photo_ids=[], voice_sample=True,
                      library={"kind": "presenter_voice", "presenter": pid, "situation": sid},
                      label=f"{p['name']} · voice sample")
        out.append({"key": pid, "label": params["label"], "tool": "ugc_video", "brief": presenters.voice_line(pid),
                    "usd": usd, "credits": _credits(usd), "live": False, "params": params})
    return out


def _example_config(p, prod, brief: dict, options: dict) -> dict:
    """The Create config that makes this preset's example on the demo product."""
    w = _WORDS.get(p.id, {})
    if p.tool == "ad_image":
        fill = catalog.template_prefill(p.as_template(), brief)
        return {"type": "image", "product": prod["id"], "preset": p.id, "versions": 1,
                "items": [w.get("headline") or (fill.get("items") or [p.name])[0]],
                "body": fill.get("body") or "", "aspects": [p.aspect or "4:5"],
                "subline": w.get("subline") or "", "cta": w.get("cta") or "", "layers": w.get("layers") or {}}
    if p.kind == "recipe":
        script = (_SCRIPTS.get(p.id) or ("", ""))[1]
        cfg = {"type": p.ad_type, "product": prod["id"], "preset": p.id, "items": [script], "body": "",
               "duration": p.price.get("duration") or 8, "aspects": ["9:16"]}
        if p.ad_type == "ugc":
            who = str(options.get("presenter") or "maya")
            cfg.update(presenters=[who],
                       situation=p.extra.get("situation") or str(options.get("situation") or "")
                       or presenters.default_situation(who))
        else:
            cfg["voices"] = [str(options.get("voice") or "narrator_bright")]
        return cfg
    return {"type": "product_video", "product": prod["id"], "items": [catalog.fill(p.recipe, brief)],
            "duration": p.price.get("duration") or 5}


def _ready_demo(conn, ws, demo_id: str, make: bool):
    """The demo product's row when it is set up (with its photo). `make` sets it up from its packshot (generate);
    without it (a quote writes nothing) None means "the packshot is there, the product isn't yet"."""
    import products
    if make:
        return ensure_demo(conn, ws, demo_id)
    if demo_id not in DEMO:
        raise LibraryError(f"Unknown demo product {demo_id!r}. Use one of: {', '.join(DEMO_IDS)}.")
    row = demo_product(conn, ws, demo_id)
    if row is not None and products.photos_for(conn, ws["id"], row["id"]):
        return row
    if _latest_done(conn, ws["id"], "demo_product", demo=demo_id) is None:
        raise LibraryError(f"{DEMO[demo_id]['name']} has no packshot yet. Make it first: kind demo_products, "
                           f"ids [\"{demo_id}\"].")
    return None


def _preset_items(conn, ws, ids, options, video: bool, make: bool = False) -> list[dict]:
    import create_routes
    import products
    pool = [p for p in presets.LIBRARY if p.kind != "type" and p.tool != "motion_copy"
            and (p.tool != "ad_image") == video and presets.runnable(p)]
    by_id = {p.id: p for p in pool}
    out = []
    for pid in _ids(ids, by_id, "presets"):
        p = by_id.get(pid)
        if p is None:
            pre = presets.get(pid)
            if pre is not None and not presets.runnable(pre):
                raise LibraryError(f"{pre.name} needs the keyframe-first video pipeline, which isn't built yet.")
            raise LibraryError(f"{pid!r} isn't {'a video' if video else 'an image'} preset that can be made here.")
        if options.get("only_missing") and (presets.example_video_url(pid) if video else presets.example_url(pid)):
            continue
        demo = str(options.get("demo") or demo_for(p))
        prod = _ready_demo(conn, ws, demo, make)
        tag = {"kind": "preset_video" if video else "preset_example", "preset": p.id, "demo": demo}
        if prod is None:
            # a quote before the demo product is set up: the preset's own table price (what expand() gives too)
            out.append({"key": pid, "label": f"{p.name} · example", "tool": p.tool, "brief": "", "usd": round(p.cost_usd(), 4),
                        "credits": p.price_credits(), "live": False, "params": {"library": tag}, "template": p.id})
            continue
        cfg = _example_config(p, prod, products.brief_for(conn, ws, prod["id"]), options)
        try:
            _meta, jobs = create_routes.expand(conn, cfg)
        except create_routes.ConfigError as e:
            raise LibraryError(f"{p.name}: {e}") from None
        j = jobs[0]
        params = dict(j["params"], preset=p.id, label=f"{p.name} · example", hook=j["hook"][:200], library=tag)
        if video and params.get("presenter"):
            params.update(_presenter_still_ref(conn, ws, params["presenter"], params.get("situation") or ""))
        out.append({"key": pid, "label": params["label"], "tool": j["tool"], "brief": j["brief"], "usd": j["usd"],
                    "credits": j["credits"], "live": False, "params": params, "template": p.id,
                    "product_id": prod["id"]})
    return out


def items(conn, ws, kind: str, ids, options=None, make: bool = False) -> list[dict]:
    """The jobs a run would start: [{key, label, tool, brief, params, usd, credits}]. Raises LibraryError. Only
    `make` (generate) may write: it sets a demo product up in the workspace from its packshot the first time."""
    options = options if isinstance(options, dict) else {}
    if kind not in KINDS:
        raise LibraryError(f"kind is one of: {', '.join(KINDS)}.")
    if kind == "demo_products":
        out = _demo_items(conn, ws, ids, options)
    elif kind == "presenter_stills":
        out = _still_items(conn, ws, ids, options)
    elif kind == "presenter_voices":
        out = _voice_items(conn, ws, ids, options)
    else:
        out = _preset_items(conn, ws, ids, options, video=kind == "preset_videos", make=make)
    if not out:
        raise LibraryError("Nothing to make: every one of those already has its example.")
    if len(out) > MAX_JOBS:
        raise LibraryError(f"That's {len(out)} jobs; one run makes at most {MAX_JOBS}. Ask for fewer at a time.")
    return out


def quote(conn, ws, kind: str, ids, options=None) -> dict:
    """What a run costs, before anything is made: total credits and our estimate of the vendor's bill in dollars."""
    its = items(conn, ws, kind, ids, options)
    total = sum(i["credits"] for i in its)
    bal = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone())
    return {"kind": kind, "count": len(its), "total_credits": total,
            "vendor_usd": round(sum(i["usd"] for i in its), 2),
            "vendor_usd_basis": "the vendor's live estimate" if its and all(i["live"] for i in its) else
                                "our price table (the vendor's bill may differ; the worker refuses a job whose "
                                "live estimate is more than 25% over it)",
            "credits_available": bal, "short_by": max(0, total - bal),
            "lines": [{"key": i["key"], "label": i["label"], "tool": i["tool"], "credits": i["credits"],
                       "usd": i["usd"], "starts_from": i.get("after") or i["params"].get("ref_static")
                       or (f"job {i['params']['ref_job']}" if i["params"].get("ref_job") else None)} for i in its]}


def generate(conn, ws, kind: str, ids, options=None, confirm_total_credits=None) -> dict:
    """Start a run. Nothing starts unless `confirm_total_credits` is the total quote() gives for the same arguments
    and the workspace can pay for all of it. The jobs are queued like any ad's (core.queue_job)."""
    import core
    its = items(conn, ws, kind, ids, options)            # priced exactly as the quote was (nothing written yet)
    total = sum(i["credits"] for i in its)
    if str(confirm_total_credits) != str(total):
        raise LibraryError(f"The total is {total:,} credits. Quote it again and confirm that number; nothing was started.")
    bal = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone())
    if bal < total:
        raise LibraryError(f"That needs {total:,} credits and this workspace has {bal:,}. Nothing was started.")
    its = items(conn, ws, kind, ids, options, make=True)  # now for real: demo products are set up on first use
    if sum(i["credits"] for i in its) != total:
        raise LibraryError("The total changed while this was starting. Quote it again; nothing was started.")
    video = any(i["tool"] != "library_still" and i["tool"] != "ad_image" for i in its)
    bid = conn.execute("INSERT INTO batches (workspace_id, user_id, product_id, ad_type, title, config_json, credits, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (ws["id"], g.user["id"], None, "ugc" if video else "image", f"Library: {kind.replace('_', ' ')}",
                        json.dumps({"library": kind, "ids": [i["key"] for i in its]}), 0, db.now())).lastrowid
    conn.commit()
    made, queued, spent = {}, [], 0
    for i in its:
        params = dict(i["params"])
        if i.get("after"):
            if i["after"] not in made:          # the first still wasn't queued: don't start the ones that need it
                continue
            params["ref_job"] = made[i["after"]]
        jid = core.queue_job(conn, i["tool"], i.get("template"), i["brief"], params, i["usd"], i["credits"], quiet=True,
                             product_id=i.get("product_id"), batch_id=bid)
        if not jid:
            break
        made[i["key"]] = jid
        queued.append({"key": i["key"], "job_id": jid, "credits": i["credits"]})
        spent += i["credits"]
    conn.execute("UPDATE batches SET credits=? WHERE id=?", (spent, bid))
    conn.commit()
    return {"kind": kind, "batch_id": bid, "queued": len(queued), "credits_charged": spent, "jobs": queued,
            "not_started": [i["key"] for i in its if i["key"] not in made]}


def overview(conn, ws_id: int) -> dict:
    """For the admin page: what exists under static/ and what has been made here, per kind."""
    roster = presenters.roster()
    stills = sum(1 for p in roster for s in p["situations"])
    return {"presenters": len(roster), "situations": stills,
            "stills_in_static": sum(1 for p in roster for s in p["situations"] if s["still"]),
            "voices_in_static": sum(1 for p in roster if p["voice_sample"]),
            "image_presets": sum(1 for p in presets.LIBRARY if p.kind != "type" and p.tool == "ad_image"),
            "video_presets": sum(1 for p in presets.LIBRARY if p.kind != "type" and p.tool != "ad_image"),
            "video_presets_today": sum(1 for p in presets.LIBRARY if p.kind != "type" and p.tool not in
                                       ("ad_image", "motion_copy") and presets.runnable(p)),
            "examples_in_static": sum(1 for p in presets.LIBRARY if presets.example_url(p.id) or
                                      presets.example_video_url(p.id)),
            "made_here": len(assets(conn, ws_id))}

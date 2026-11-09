"""A plan's videos as a PAUSED campaign on a full-screen video platform: Snapchat (roadmap M6.1) and TikTok (M6.2).

launches.py runs it: the same approval card, approve(), ledger, saga, compensation and rules (D2 paid plan, D3 daily
ceiling in the account's currency, library assets only). This file has what the video platforms share and what is
each one's own:

    draft()        the spec from the plan: its finished videos (this platform's first), the copy studio's copy for
                   the platform, the budget share, the connection's pixel
    apply_form()   the member's edits on the Launch page (launches.apply_form, then the platform's defaults)
    issues()       launch rules on top of adspec.validate and adpolicy: the public profile (Snapchat) or identity
                   (TikTok), the pixel and the event, whole countries only, the real shape and size of each video
                   file, TikTok's age bands, restricted categories
    make_card()    "What Campaignify will create on <platform>", written from the steps that will run

Everything here sits behind AD_LAUNCH and the platform's own flag (SNAPCHAT_ADS, TIKTOK_ADS).
"""
import json
import os
from datetime import timedelta

import adpolicy
import adspec
import launches
from adspec import Issue

PLATFORMS = ("snapchat", "tiktok")
WORDS = {"snapchat": "Snapchat", "tiktok": "TikTok"}
ADSET_WORDS = {"snapchat": "ad squad", "tiktok": "ad group"}
IDENTITY_WORDS = {"snapchat": "public profile", "tiktok": "identity"}
CTA_DEFAULT = {"sales": "SHOP_NOW", "leads": "SIGN_UP", "traffic": "LEARN_MORE", "awareness": "LEARN_MORE"}
EVENTS = {"snapchat": ("Purchase", "Lead", "CompleteRegistration", "AddToCart", "ViewContent"),
          "tiktok": ("Purchase", "Lead", "CompleteRegistration", "AddToCart", "InitiateCheckout", "ViewContent")}


def module(platform: str):
    if platform == "snapchat":
        from adplatforms import snapchat
        return snapchat
    if platform == "tiktok":
        from adplatforms import tiktok
        return tiktok
    raise KeyError(platform)


def pixels_for(conn, ws_id: int, conn_row) -> list:
    """The pixels a launch on this connection can optimize for: the connection's, and on TikTok also pixels the member
    added by hand in Conversion tracking (the same pixel codes; they aren't tied to a connection)."""
    if conn_row is None:
        return []
    rows = list(launches.pixels_of(conn, ws_id, conn_row["id"]))
    if conn_row["platform"] == "tiktok":
        have = {r["external_pixel_id"] for r in rows}
        rows += [r for r in conn.execute("SELECT * FROM pixels WHERE workspace_id=? AND platform='tiktok' AND "
                                         "connection_id IS NULL ORDER BY id", (int(ws_id),)).fetchall()
                 if r["external_pixel_id"] not in have]
    return rows


# ---------------------------------------------------------------- what the plan has

def plan_videos(conn, ws_id: int, cid: int, platform: str) -> list:
    """The plan's finished video ads, this platform's first: [{"asset_id", "kind", "angle", "path", "platform"}]."""
    rows = conn.execute("SELECT a.angle, a.platform, o.id AS oid, o.kind, o.path FROM campaign_ads a JOIN jobs j ON "
                        "j.id=a.job_id JOIN outputs o ON o.job_id=j.id WHERE a.campaign_id=? AND a.workspace_id=? AND "
                        "o.kind='video' AND j.status='done' AND j.deleted_at IS NULL ORDER BY a.platform!=?, a.id, o.id",
                        (int(cid), int(ws_id), platform)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["oid"] not in seen:
            seen.add(r["oid"])
            out.append({"asset_id": r["oid"], "kind": "video", "angle": r["angle"] or "", "path": r["path"],
                        "platform": r["platform"]})
    return out


def copy_for(plan: dict, platform: str, angle: str) -> dict:
    """{"primary_text", "headline", "description"} from the plan's copy: this angle's variant for this platform, then
    for Meta, then the first variant that passed the checks (this platform's, then Meta's)."""
    by = {p: [v for cp in plan.get("copy") or [] if cp.get("platform") == p for v in cp.get("variants") or []]
          for p in (platform, "meta")}
    v = next((x for p in (platform, "meta") for x in by[p] if x.get("angle") == angle and x.get("ok")), None) or \
        next((x for p in (platform, "meta") for x in by[p] if x.get("ok")), None) or \
        next((x for p in (platform, "meta") for x in by[p]), None)
    out = {"primary_text": "", "headline": "", "description": ""}
    for f in (v or {}).get("fields") or []:
        n = (f.get("name") or "").lower()
        key = "headline" if "headline" in n or "title" in n else \
            "primary_text" if "primary" in n or n in ("text", "caption", "body", "ad text") else None
        if key and not out[key]:
            out[key] = (f.get("text") or "").strip()
    return out


def _fit(text: str, limit: int) -> str:
    """Cut to `limit` characters at a word, without a dangling space."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip(" ,;:")


# ---------------------------------------------------------------- the draft

def draft(conn, ws, c, conn_row, platform: str) -> dict:
    """A CampaignSpec dict for this plan on this connection, before the member's edits."""
    mod = module(platform)
    brief, plan = json.loads(c["brief_json"] or "{}"), json.loads(c["plan_json"] or "{}")
    objective = launches.GOAL_TO_OBJECTIVE.get((brief.get("goal") or "sales").lower(), "sales")
    link = (brief.get("link") or "").strip()
    link = link if link.startswith("https://") else ""
    cur = conn_row["currency"] if conn_row is not None and conn_row["currency"] else "USD"
    pxs = pixels_for(conn, ws["id"], conn_row)
    start = launches._local_today(conn_row["timezone"] if conn_row is not None else None) + timedelta(days=1)
    import campaign_budget
    days = int(brief.get("budget_days") or 0) if campaign_budget.structured(brief) else 0
    hl = adspec.TEXT_LIMITS[platform]["headline"][1] or 100
    tl = adspec.TEXT_LIMITS[platform]["primary_text"][1] or 100
    creatives = []
    for i, a in enumerate(plan_videos(conn, ws["id"], c["id"], platform)[:10]):
        cp = copy_for(plan, platform, a["angle"])
        tiktok = platform == "tiktok"
        creatives.append({"kind": "video", "asset_id": a["asset_id"], "name": (a["angle"] or f"Ad {i + 1}")[:80],
                          "primary_text": _fit(cp["primary_text"] or cp["headline"], tl) if tiktok else "",
                          "headline": "" if tiktok else _fit(cp["headline"] or cp["primary_text"], hl),
                          "description": "", "cta": CTA_DEFAULT[objective], "destination_url": link, "utm": mod.UTM,
                          "aspect": launches._aspect(a["path"] + ".jpg")})
    d = {"objective": objective, "platform": platform, "name": (c["name"] or "Campaignify campaign")[:120],
         "daily_budget_minor": launches._daily_from_brief(brief, plan, cur, platform), "currency": cur,
         "start_at": start.isoformat(), "end_at": (start + timedelta(days=days)).isoformat() if days else None,
         "geo": {"countries": ["US"]}, "audience": {"age_min": 18, "age_max": 65, "genders": ["all"]},
         "placements": "auto",
         "conversion": ({"pixel_id": pxs[0]["external_pixel_id"], "event": "Purchase" if objective == "sales" else "Lead",
                         "landing_url": link} if pxs and objective in ("sales", "leads") else None),
         "creatives": creatives, "special_ad_categories": [], "ai_disclosure": False, "google": None,
         "vertical": "general", "claims_substantiated": False}
    spec, _ = adspec.from_dict(d)
    if spec is not None and spec.creatives:
        prov = adpolicy.provenance(conn, ws["id"], [x.asset_id for x in spec.creatives if x.asset_id])
        vertical, _why = adpolicy.derive_vertical(spec, prov)
        if vertical in adspec.VERTICALS:
            d["vertical"] = vertical
    return d


def apply_form(base: dict, form, videos: list) -> dict:
    """The member's edits on top of `base`: launches.apply_form, then this platform's UTM on every ad and nothing the
    platform doesn't show (Snapchat: primary text, description; TikTok: headline, description)."""
    platform = base.get("platform")
    d = launches.apply_form(base, form, videos)
    mod = module(platform)
    for c in d.get("creatives") or []:
        c["utm"] = mod.UTM
        c["kind"] = "video"
        if platform == "snapchat":
            c["primary_text"] = c["description"] = ""
        if platform == "tiktok":
            c["headline"] = c["description"] = ""
    d["placements"] = "auto"
    return d


# ---------------------------------------------------------------- rules

def _connection_pixels(conn, ws_id: int, conn_row) -> set:
    return {p["external_pixel_id"] for p in pixels_for(conn, ws_id, conn_row)}


def issues(conn, ws, spec, conn_row, assets: dict) -> list:
    """Launch rules on top of adspec.validate and adpolicy.lint (launches.launch_issues adds plan, ceiling,
    connection and library checks)."""
    out = []
    add = lambda *a: out.append(Issue(*a))  # noqa: E731
    p, word = spec.platform, WORDS[spec.platform]
    mod = module(p)
    if spec.currency != (conn_row["currency"] or spec.currency):
        add("block", "launch.currency", f"The ad account bills in {conn_row['currency']}, not {spec.currency}.",
            "Reload the page.", "currency")
    if not conn_row["page_id"]:
        add("block", f"{p}.no_profile", f"Ads on {word} run from a {IDENTITY_WORDS[p]}, and none is picked.",
            f"Make one in {word} Ads Manager if you have none, then reconnect the ad account and pick it.", "connection")
    conv = spec.conversion or {}
    if conv.get("pixel_id") and conv["pixel_id"] not in _connection_pixels(conn, ws["id"], conn_row):
        add("block", "launch.pixel_unknown", "That pixel isn't one of this ad account's pixels.",
            "Pick a pixel from the list.", "conversion.pixel_id")
    if spec.objective in ("sales", "leads") and conv.get("event") and conv["event"] not in EVENTS[p]:
        add("block", f"{p}.event", f"{word} can't optimize for {conv['event']} here.",
            "Pick Purchase, Lead or another event from the list.", "conversion.event")
    geo = spec.geo or {}
    if geo.get("zips") or geo.get("regions") or geo.get("cities"):
        add("block", f"{p}.geo_detail", f"On {word}, Campaignify targets whole countries for now.",
            f"Use country codes here, then narrow it in {word} Ads Manager before you turn it on.", "geo")
    cats = set(spec.special_ad_categories or [])
    if "social_issues" in cats:
        add("block", f"{p}.political", f"Political and issue ads on {word} need {word}'s own approval first.",
            f"Make them in {word} Ads Manager after you are approved there.", "special_ad_categories")
    if cats & {"housing", "credit", "employment"}:
        add("warn", f"{p}.restricted_category", f"{word} limits targeting for housing, credit and job ads.",
            f"Check the {ADSET_WORDS[p]}'s targeting in {word} Ads Manager before you turn it on.", "special_ad_categories")
    aud = spec.audience or {}
    if p == "tiktok":
        bad = [c for c in geo.get("countries") or [] if c not in mod.COUNTRIES]
        if bad:
            add("block", "tiktok.country_unsupported", f"Campaignify can't target {', '.join(bad)} on TikTok yet.",
                "Use other countries, or add it in TikTok Ads Manager after the campaign is made.", "geo.countries")
        groups = mod.age_groups(aud)
        lo, hi = mod.band_ages(groups)
        if (lo, hi if hi < 100 else 65) != (int(aud.get("age_min", 18)), int(aud.get("age_max", 65))):
            add("warn", "tiktok.age_band", f"TikTok targets age bands, so this reaches ages {lo} to "
                + ("55 and over" if hi >= 100 else str(hi)) + ".", "Set the ages on a band's edges (13, 18, 25, 35, 45, "
                "55) to match exactly.", "audience")
    if p == "snapchat" and mod.OLDEST <= int(aud.get("age_max", 65)) < 65:
        add("warn", "snapchat.age_band", "Snapchat's oldest age band is 50 and over, so the oldest age is left open.",
            "Set the oldest age under 50 to cap it.", "audience.age_max")
    for i, c in enumerate(spec.creatives):
        w = f"creatives[{i}]"
        a = assets.get(c.asset_id) if c.asset_id else None
        if not a or not a.get("path") or not os.path.exists(a["path"]):
            continue                        # launches.launch_issues already says why
        real = launches._aspect(a.get("poster") or (a["path"] + ".jpg"))
        if p == "snapchat" and real != "9:16":
            add("block", "snapchat.file_not_vertical", f"Ad {i + 1}'s video is {real}. Snapchat takes 9:16 only.",
                "Make a 9:16 version of it, or untick it.", f"{w}.aspect")
        if p == "tiktok" and real not in adspec.ASPECTS_OK["tiktok"]:
            add("block", "tiktok.file_shape", f"Ad {i + 1}'s video is {real}. TikTok takes 9:16, 1:1 or 16:9.",
                "Make a 9:16 version of it, or untick it.", f"{w}.aspect")
        if p == "tiktok" and not (a.get("poster") and os.path.exists(a["poster"])):
            add("block", "tiktok.no_cover", f"Ad {i + 1}'s video has no cover picture, which TikTok needs.",
                "Make the video again, or untick it.", w)
        if p == "snapchat" and os.path.getsize(a["path"]) > mod.VIDEO_MAX_BYTES:
            add("block", "snapchat.file_large", f"Ad {i + 1}'s video is over 32 MB, the most Campaignify sends to "
                "Snapchat in one go.", "Make a shorter version of it, or untick it.", w)
        if not (c.headline or "").strip() and p == "snapchat":
            add("block", "snapchat.headline_missing", f"Ad {i + 1} needs a headline (up to 34 characters).",
                "Write one.", f"{w}.headline")
    return out


# ---------------------------------------------------------------- the approval card

def make_card(conn, ws, spec, conn_row, steps: list, prov: dict) -> dict:
    p, word = spec.platform, WORDS[spec.platform]
    mod = module(p)
    cur = spec.currency
    money = launches.money(spec.daily_budget_minor, cur)
    aud, geo = spec.audience or {}, spec.geo or {}
    genders = {"women": "women", "men": "men"}.get((aud.get("genders") or ["all"])[0], "all genders")
    where = ", ".join(geo.get("countries") or []) or "no location"
    lo, hi = aud.get("age_min", 18), aud.get("age_max", 65)
    if p == "tiktok":
        blo, bhi = mod.band_ages(mod.age_groups(aud))
        ages = f"ages {blo} and over" if bhi >= 100 else f"ages {blo} to {bhi}"
    else:
        ages = f"ages {lo} and over" if hi >= mod.OLDEST else f"ages {lo} to {hi}"
    conv = spec.conversion or {}
    px_name = None
    if conn_row is not None and conv.get("pixel_id"):
        row = next((x for x in pixels_for(conn, ws["id"], conn_row) if x["external_pixel_id"] == conv["pixel_id"]), None)
        px_name = (row["name"] if row else None) or f"pixel {conv['pixel_id']}"
    ads = [s for s in steps if s["kind"] == "ad"]
    names = "; ".join(s["name"] for s in ads) + "."
    plural = "s" if len(ads) != 1 else ""
    if p == "tiktok":
        g, _, ev = mod.goal(spec)
        goal = f"optimizes for {mod.EVENT_WORDS.get(ev or g, (ev or g).lower())}" + (f" on {px_name}" if ev and px_name else "")
        lines = [f"1 campaign: {spec.name}. {launches.OBJECTIVE_WORDS[spec.objective]}, turned off (PAUSED).",
                 f"1 {ADSET_WORDS[p]}: {money} a day, {where}, {ages}, {genders}, the TikTok placement, {goal}. "
                 "On, inside the turned-off campaign.",
                 f"{len(ads)} ad{plural}, on inside the turned-off campaign, showing as "
                 f"{(conn_row['page_name'] if conn_row is not None else '') or 'your identity'}: {names}"]
    else:
        g = mod.goal(spec)
        goal = f"optimizes for {mod.GOAL_WORDS.get(g, g.lower())}" + (f" on {px_name}" if px_name and g.startswith("PIXEL")
                                                                      else "")
        lines = [f"1 campaign: {spec.name}. {launches.OBJECTIVE_WORDS[spec.objective]}, PAUSED.",
                 f"1 {ADSET_WORDS[p]}: {money} a day, {where}, {ages}, {genders}, automatic placements, {goal}. PAUSED.",
                 f"{len(ads)} ad{plural}, PAUSED: {names}"]
    for i, c in enumerate(spec.creatives, start=1):
        if f"ad:{i}" in {s["key"] for s in ads}:
            said = c.primary_text if p == "tiktok" else c.headline
            lines.append(f'Ad {i}: "{said}", button {launches.CTA_WORDS.get(c.cta, c.cta)}, opens {c.destination_url}.')
    presenter = [i + 1 for i, c in enumerate(spec.creatives) if c.ai_presenter or c.synthetic_voice]
    made = [i + 1 for i, c in enumerate(spec.creatives) if i + 1 not in presenter
            and (prov.get(c.asset_id) or {}).get("ai_generated")]
    if presenter or made:
        parts = []
        if presenter:
            parts.append(f"{launches._ad_list(presenter)} {'has' if len(presenter) == 1 else 'have'} an AI presenter or AI voice")
        if made:
            parts.append(f"{launches._ad_list(made)} {'is' if len(made) == 1 else 'are'} AI-made media")
        disclosure = ("AI disclosure: " + "; ".join(parts) + (
            ". Before you turn the campaign on, turn on TikTok's AI-generated content label on "
            + ("that ad" if len(presenter) + len(made) == 1 else "those ads") + " in TikTok Ads Manager."
            if p == "tiktok" else f". {word} has no AI label setting Campaignify can set, so keep the AI label on the video "
            "itself."))
    else:
        disclosure = "AI disclosure: none of these ads use AI-made media."
    start, end = launches._day(spec.start_at), launches._day(spec.end_at)
    schedule = (f"Starts {start}" if start else "Starts when you turn it on") + (f", ends {end}." if end else ", no end date.")
    objs = [{"kind": s["kind"], "name": s["name"], "key": s["key"]} for s in steps if s["kind"] != "upload"]
    account = (f"{conn_row['account_name'] or word + ' ad account'} ({conn_row['external_account_id']})"
               if conn_row is not None else f"No {word} ad account connected")
    page = (f"{IDENTITY_WORDS[p].capitalize()}: {conn_row['page_name'] or conn_row['page_id']}"
            if conn_row is not None and conn_row["page_id"] else f"No {IDENTITY_WORDS[p]} picked")
    return {"platform": p, "account": account, "page": page, "lines": lines, "budget": f"{money} a day",
            "schedule": schedule, "disclosure": disclosure, "ai": bool(presenter or made), "objects": objs,
            "uploads": sum(1 for s in steps if s["kind"] == "upload"),
            "footer": ("Nothing runs until you turn the campaign on. Inside it, the ad group and ads are on, so turning "
                       "the campaign on is all it takes." if p == "tiktok" else "Nothing runs until you turn it on.")}

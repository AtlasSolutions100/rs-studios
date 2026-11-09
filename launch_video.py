"""A plan's videos as a PAUSED campaign on a full-screen video platform: Snapchat (roadmap M6.1).

launches.py runs it: the same approval card, approve(), ledger, saga, compensation and rules (D2 paid plan, D3 daily
ceiling in the account's currency, library assets only). This file has what the video platforms share and what is
each one's own:

    draft()        the spec from the plan: its finished videos (this platform's first), the copy studio's copy for
                   the platform, the budget share, the connection's pixel
    apply_form()   the member's edits on the Launch page (launches.apply_form, then the platform's defaults)
    issues()       launch rules on top of adspec.validate and adpolicy: the public profile, the pixel and the event,
                   whole countries only, the real shape and size of each video file, restricted categories
    make_card()    "What Campaignify will create on <platform>", written from the steps that will run

Everything here sits behind AD_LAUNCH and the platform's own flag (SNAPCHAT_ADS).
"""
import json
import os
from datetime import timedelta

import adpolicy
import adspec
import launches
from adspec import Issue

PLATFORMS = ("snapchat",)
WORDS = {"snapchat": "Snapchat"}
ADSET_WORDS = {"snapchat": "ad squad"}
IDENTITY_WORDS = {"snapchat": "public profile"}
CTA_DEFAULT = {"sales": "SHOP_NOW", "leads": "SIGN_UP", "traffic": "LEARN_MORE", "awareness": "LEARN_MORE"}
EVENTS = {"snapchat": ("Purchase", "Lead", "CompleteRegistration", "AddToCart", "ViewContent")}


def module(platform: str):
    if platform == "snapchat":
        from adplatforms import snapchat
        return snapchat
    raise KeyError(platform)


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
    pxs = launches.pixels_of(conn, ws["id"], conn_row["id"]) if conn_row is not None else []
    start = launches._local_today(conn_row["timezone"] if conn_row is not None else None) + timedelta(days=1)
    import campaign_budget
    days = int(brief.get("budget_days") or 0) if campaign_budget.structured(brief) else 0
    hl = adspec.TEXT_LIMITS[platform]["headline"][1] or 100
    creatives = []
    for i, a in enumerate(plan_videos(conn, ws["id"], c["id"], platform)[:10]):
        cp = copy_for(plan, platform, a["angle"])
        creatives.append({"kind": "video", "asset_id": a["asset_id"], "name": (a["angle"] or f"Ad {i + 1}")[:80],
                          "primary_text": "", "headline": _fit(cp["headline"] or cp["primary_text"], hl),
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
    """The member's edits on top of `base`: launches.apply_form, then this platform's UTM on every ad and nothing
    Snapchat doesn't show (primary text, description)."""
    platform = base.get("platform")
    d = launches.apply_form(base, form, videos)
    mod = module(platform)
    for c in d.get("creatives") or []:
        c["utm"] = mod.UTM
        c["kind"] = "video"
        if platform == "snapchat":
            c["primary_text"] = c["description"] = ""
    d["placements"] = "auto"
    return d


# ---------------------------------------------------------------- rules

def _connection_pixels(conn, ws_id: int, conn_row) -> set:
    return {p["external_pixel_id"] for p in launches.pixels_of(conn, ws_id, conn_row["id"])}


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
    ages = f"ages {lo} and over" if p == "snapchat" and hi >= mod.OLDEST else f"ages {lo} to {hi}"
    conv = spec.conversion or {}
    px_name = None
    if conn_row is not None and conv.get("pixel_id"):
        row = next((x for x in launches.pixels_of(conn, ws["id"], conn_row["id"])
                    if x["external_pixel_id"] == conv["pixel_id"]), None)
        px_name = (row["name"] if row else None) or f"pixel {conv['pixel_id']}"
    g = mod.goal(spec)
    goal = f"optimizes for {mod.GOAL_WORDS.get(g, g.lower())}" + (f" on {px_name}" if px_name and g.startswith("PIXEL")
                                                                  else "")
    ads = [s for s in steps if s["kind"] == "ad"]
    lines = [f"1 campaign: {spec.name}. {launches.OBJECTIVE_WORDS[spec.objective]}, PAUSED.",
             f"1 {ADSET_WORDS[p]}: {money} a day, {where}, {ages}, {genders}, automatic placements, {goal}. PAUSED.",
             f"{len(ads)} ad{'s' if len(ads) != 1 else ''}, PAUSED: " + "; ".join(s["name"] for s in ads) + "."]
    by_key = {s["key"]: s for s in steps}
    for i, c in enumerate(spec.creatives, start=1):
        cr = (by_key.get(f"creative:{i}") or {}).get("request", {}).get("creative") or {}
        if cr:
            lines.append(f'Ad {i}: "{c.headline}", button {launches.CTA_WORDS.get(c.cta, c.cta)}, opens {c.destination_url}.')
    presenter = [i + 1 for i, c in enumerate(spec.creatives) if c.ai_presenter or c.synthetic_voice]
    made = [i + 1 for i, c in enumerate(spec.creatives) if i + 1 not in presenter
            and (prov.get(c.asset_id) or {}).get("ai_generated")]
    if presenter or made:
        parts = []
        if presenter:
            parts.append(f"{launches._ad_list(presenter)} {'has' if len(presenter) == 1 else 'have'} an AI presenter or AI voice")
        if made:
            parts.append(f"{launches._ad_list(made)} {'is' if len(made) == 1 else 'are'} AI-made media")
        disclosure = ("AI disclosure: " + "; ".join(parts) + f". {word} has no AI label setting Campaignify can set, so "
                      "keep the AI label on the video itself.")
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
            "footer": "Nothing runs until you turn it on."}

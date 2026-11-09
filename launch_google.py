"""A plan as a PAUSED Google Ads campaign (roadmap M3.2 Search with a responsive search ad, M3.3 Performance Max).

launches.py runs it: the same approval card, approve(), ledger, saga, compensation and rules (D2 paid plan,
D3 daily ceiling in the account's currency, library assets only). This file has what is Google's own:

    draft()        the spec from the plan: the planner's keyword list (service path), the copy studio's Google
                   headlines and descriptions, the plan's images and the site logo for Performance Max
    apply_form()   the member's edits on the Launch page
    issues()       Google launch rules on top of adspec.validate and adpolicy (locations Campaignify can target,
                   language, political ads, bidding without conversion tracking, logos that Google takes)
    make_card()    "What Campaignify will create on Google", written from the steps that will run

Everything here sits behind AD_LAUNCH and GOOGLE_ADS (adplatforms.google.enabled).
"""
import json
import os
import re
from datetime import timedelta

import adpolicy
import adspec
import launches
from adplatforms import google

MAX_KEYWORDS = 50
MAX_IMAGES = 5
MAX_LOGOS = 5
TYPE_WORDS = {"search": "Search", "pmax": "Performance Max"}
BIDDING_WORDS = {"clicks": "Maximize clicks", "conversions": "Maximize conversions"}


# ---------------------------------------------------------------- what the plan has

def copy_from_plan(plan: dict) -> tuple:
    """(headlines <= 30 chars, descriptions <= 90 chars) from the copy studio's Google copy, checks passed first,
    duplicates dropped. The planner writes "Headline 1..3" and "Description 1..2" per variant."""
    heads, descs, seen = [], [], set()
    variants = [v for cp in plan.get("copy") or [] if cp.get("platform") == "google" for v in cp.get("variants") or []]
    variants.sort(key=lambda v: not v.get("ok"))
    for v in variants:
        for f in v.get("fields") or []:
            n, t = (f.get("name") or "").lower(), re.sub(r"\s+", " ", (f.get("text") or "")).strip()
            if not t or t.lower() in seen:
                continue
            if "headline" in n and len(t) <= 30:
                heads.append(t)
            elif "description" in n and len(t) <= 90:
                descs.append(t)
            else:
                continue
            seen.add(t.lower())
    return heads[:15], descs[:4]


def keywords_from_plan(plan: dict) -> tuple:
    """(keywords, negatives) as [{"term", "match"}] from the planner's list (plan.final.keywords)."""
    kw = (plan.get("final") or {}).get("keywords") or {}
    items = [{"term": str(k.get("term") or "")[:80], "match": k.get("match") if k.get("match") in google.MATCH else "phrase"}
             for k in kw.get("items") or [] if isinstance(k, dict) and k.get("term")]
    neg = [{"term": str(t)[:80], "match": "phrase"} for t in kw.get("negatives") or [] if str(t).strip()]
    return items[:MAX_KEYWORDS], neg[:MAX_KEYWORDS]


def plan_images(conn, ws_id: int, cid: int) -> list:
    """The plan's finished image ads (any platform, Google's first): [{"asset_id", "kind", "angle", "path"}]."""
    rows = conn.execute("SELECT a.angle, a.platform, o.id AS oid, o.kind, o.path FROM campaign_ads a JOIN jobs j ON "
                        "j.id=a.job_id JOIN outputs o ON o.job_id=j.id WHERE a.campaign_id=? AND a.workspace_id=? AND "
                        "o.kind='image' AND j.status='done' AND j.deleted_at IS NULL ORDER BY a.platform!='google', a.id, "
                        "o.id", (int(cid), int(ws_id))).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["oid"] not in seen:
            seen.add(r["oid"])
            out.append({"asset_id": r["oid"], "kind": "image", "angle": r["angle"] or "", "path": r["path"]})
    return out


def logo_choices(conn, ws_id: int, product_id) -> list:
    """Photos that can be a logo: the product's photos, the site's logo first. [{"id", "name", "path", "kind",
    "padded", "site"}]; kind None when Google can't take it (too small)."""
    if not product_id:
        return []
    p = conn.execute("SELECT import_json FROM products WHERE id=? AND workspace_id=?", (product_id, ws_id)).fetchone()
    site_logo = None
    if p is not None:
        try:
            site_logo = ((json.loads(p["import_json"] or "{}") or {}).get("site") or {}).get("logo_photo_id")
        except (ValueError, TypeError):
            site_logo = None
    rows = conn.execute("SELECT * FROM photos WHERE workspace_id=? AND product_id=? ORDER BY id", (ws_id, product_id)).fetchall()
    out = []
    for r in rows:
        k = google.logo_kind(r["path"]) if r["path"] and os.path.exists(r["path"]) else None
        out.append({"id": r["id"], "name": r["filename"] or f"Photo {r['id']}", "path": r["path"],
                    "kind": k[0] if k else None, "padded": bool(k and k[1]),
                    "site": r["id"] == site_logo or (r["filename"] or "").lower().startswith("logo")})
    out.sort(key=lambda x: (not x["site"], x["id"]))
    return out


def logo_assets(conn, ws_id: int, spec) -> dict:
    """{"logo:<photo id>": {"path", "field", "padded"}} for the spec's logos (this workspace's photos only)."""
    out = {}
    for pid in (spec.google or {}).get("logos") or []:
        if not str(pid).isdigit():
            continue
        r = conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (int(pid), int(ws_id))).fetchone()
        if r is None or not r["path"] or not os.path.exists(r["path"]):
            continue
        k = google.logo_kind(r["path"])
        out[f"logo:{pid}"] = {"path": r["path"], "field": k[0] if k else None, "padded": bool(k and k[1]),
                              "name": r["filename"] or ""}
    return out


def business_name(conn, ws, c) -> str:
    row = None
    if c["product_id"]:
        row = conn.execute("SELECT b.brand_name FROM products p JOIN brands b ON b.id=p.brand_id WHERE p.id=? AND "
                           "p.workspace_id=?", (c["product_id"], ws["id"])).fetchone()
    name = (row["brand_name"] if row is not None else None) or ws["brand_name"] or ws["name"] or ""
    return re.sub(r"\s+", " ", name).strip()[:25]


# ---------------------------------------------------------------- the draft

def draft(conn, ws, c, conn_row, typ: str = None) -> dict:
    """A CampaignSpec dict for this plan on this Google Ads connection, before the member's edits."""
    brief, plan = json.loads(c["brief_json"] or "{}"), json.loads(c["plan_json"] or "{}")
    objective = launches.GOAL_TO_OBJECTIVE.get((brief.get("goal") or "sales").lower(), "sales")
    link = (brief.get("link") or "").strip()
    link = link if link.startswith("https://") else ""
    heads, descs = copy_from_plan(plan)
    kws, negs = keywords_from_plan(plan)
    images = plan_images(conn, ws["id"], c["id"])
    if typ not in google_types():
        typ = "search" if kws or not images else "pmax"
    cur = conn_row["currency"] if conn_row is not None and conn_row["currency"] else "USD"
    start = launches._local_today(conn_row["timezone"] if conn_row is not None else None) + timedelta(days=1)
    import campaign_budget
    days = int(brief.get("budget_days") or 0) if campaign_budget.structured(brief) else 0
    logos = [x["id"] for x in logo_choices(conn, ws["id"], c["product_id"]) if x["site"] and x["kind"]][:1]
    creatives = []
    if typ == "pmax":
        for a in images[:3]:
            creatives.append({"kind": "image", "asset_id": a["asset_id"], "name": (a["angle"] or "Image")[:80],
                              "cta": "LEARN_MORE", "destination_url": link, "utm": "", "aspect": launches._aspect(a["path"])})
    long_heads = [d for d in descs if len(d) <= 90][:1] or ([brief.get("offer")[:90]] if brief.get("offer") else [])
    g = {"type": typ, "final_url": link, "keywords": kws, "negative_keywords": negs,
         "headlines": heads, "headline_pins": [0] * len(heads), "descriptions": descs, "description_pins": [0] * len(descs),
         "path1": "", "path2": "", "bidding": "conversions" if objective in ("sales", "leads") else "clicks",
         "language": "en", "search_partners": True,
         "long_headlines": long_heads, "business_name": business_name(conn, ws, c), "logos": logos}
    d = {"objective": objective, "platform": "google", "name": (c["name"] or "Campaignify campaign")[:120],
         "daily_budget_minor": launches._daily_from_brief(brief, plan, cur, "google"), "currency": cur,
         "start_at": start.isoformat(), "end_at": (start + timedelta(days=days)).isoformat() if days else None,
         "geo": {"countries": ["US"]}, "audience": {"age_min": 18, "age_max": 65, "genders": ["all"]},
         "placements": "auto", "conversion": None, "creatives": creatives, "special_ad_categories": [],
         "ai_disclosure": False, "google": g, "vertical": "general", "claims_substantiated": False}
    spec, _ = adspec.from_dict(d)
    if spec is not None:
        prov = adpolicy.provenance(conn, ws["id"], [x.asset_id for x in spec.creatives if x.asset_id])
        vertical, _why = adpolicy.derive_vertical(spec, prov)
        if vertical in adspec.VERTICALS:
            d["vertical"] = vertical
    return d


def google_types():
    return tuple(TYPE_WORDS)


KW_RX = re.compile(r'^\s*(\[(?P<e>[^\]]+)\]|"(?P<p>[^"]+)"|(?P<b>.+?))\s*$')


def parse_keywords(text: str) -> list:
    """One keyword per line, Google's way: [exact], "phrase", broad."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        m = KW_RX.match(line)
        if not m or not line.strip():
            continue
        term = re.sub(r"\s+", " ", (m.group("e") or m.group("p") or m.group("b") or "")).strip().lower()[:120]
        match = "exact" if m.group("e") else "phrase" if m.group("p") else "broad"
        if term and (term, match) not in seen:
            seen.add((term, match))
            out.append({"term": term, "match": match})
    return out[:MAX_KEYWORDS]


def keyword_lines(items) -> str:
    """The textarea form of a keyword list."""
    return "\n".join(f"[{k['term']}]" if k.get("match") == "exact" else f'"{k["term"]}"' if k.get("match") == "phrase"
                     else k["term"] for k in items or [] if isinstance(k, dict) and k.get("term"))


def _lines(form, key: str, n: int, limit: int) -> list:
    return [re.sub(r"\s+", " ", (form.get(f"{key}_{i}") or "")).strip()[:limit] for i in range(n)]


def apply_form(base: dict, form, images: list) -> dict:
    """The member's edits on the Google Launch page on top of `base`."""
    d = launches.apply_form(base, form, images if (base.get("google") or {}).get("type") == "pmax" else [])
    g = dict(d.get("google") or {})
    f = form.get
    typ = f("gtype") if f("gtype") in google_types() else g.get("type", "search")
    if f("form") == "1":
        if f("landing") is not None:
            g["final_url"] = (f("landing") or "").strip()
        if f("keywords") is not None:
            g["keywords"] = parse_keywords(f("keywords"))
        if f("negatives") is not None:
            g["negative_keywords"] = parse_keywords(f("negatives"))
        if f("bidding") in BIDDING_WORDS:
            g["bidding"] = f("bidding")
        if f("language") in google.LANGUAGES:
            g["language"] = f("language")
        g["search_partners"] = f("partners") == "1"
        for p in ("path1", "path2"):
            if f(p) is not None:
                g[p] = re.sub(r"\s+", "-", (f(p) or "").strip())[:40]
        if f("head_n") is not None:
            n = min(15, int(f("head_n") or 0) if (f("head_n") or "").isdigit() else 0)
            heads, pins = _lines(form, "h", n, 200), []
            for i in range(n):
                v = f(f"hpin_{i}") or "0"
                pins.append(int(v) if v in ("0", "1", "2", "3") else 0)
            g["headlines"] = [h for h in heads if h]
            g["headline_pins"] = [p for h, p in zip(heads, pins) if h]
        if f("desc_n") is not None:
            n = min(5, int(f("desc_n") or 0) if (f("desc_n") or "").isdigit() else 0)
            descs, pins = _lines(form, "d", n, 400), []
            for i in range(n):
                v = f(f"dpin_{i}") or "0"
                pins.append(int(v) if v in ("0", "1", "2") else 0)
            g["descriptions"] = [x for x in descs if x]
            g["description_pins"] = [p for x, p in zip(descs, pins) if x]
        if f("long_n") is not None:
            n = min(5, int(f("long_n") or 0) if (f("long_n") or "").isdigit() else 0)
            g["long_headlines"] = [x for x in _lines(form, "lh", n, 300) if x]
        if f("business") is not None:
            g["business_name"] = re.sub(r"\s+", " ", (f("business") or "")).strip()[:60]
        if f("logos_form") == "1":
            g["logos"] = [int(x) for x in form.getlist("logo") if str(x).isdigit()][:MAX_LOGOS + 1]
    if typ != g.get("type"):
        g["type"] = typ
        if typ == "search":
            d["creatives"] = []
        elif not d.get("creatives"):
            d["creatives"] = [{"kind": "image", "asset_id": a["asset_id"], "name": (a["angle"] or "Image")[:80],
                               "cta": "LEARN_MORE", "destination_url": g.get("final_url") or "", "utm": "",
                               "aspect": launches._aspect(a["path"])} for a in images[:3]]
        if typ == "pmax":
            g["bidding"] = "conversions"
    if typ == "search":
        d["creatives"] = []
    else:
        for c in d.get("creatives") or []:
            c["destination_url"] = g.get("final_url") or c.get("destination_url") or ""
            c["primary_text"] = c["headline"] = c["description"] = c["utm"] = ""
    d["google"] = g
    d["conversion"] = None
    return d


# ---------------------------------------------------------------- rules

def issues(conn, ws, spec, conn_row, assets: dict) -> list:
    """Google launch rules on top of adspec.validate and adpolicy.lint."""
    from adspec import Issue
    out = []
    add = lambda *a: out.append(Issue(*a))  # noqa: E731
    g, geo = spec.google or {}, spec.geo or {}
    typ = g.get("type") or "search"
    bad = [c for c in geo.get("countries") or [] if c not in google.COUNTRIES]
    if bad:
        add("block", "google.country_unsupported", f"Campaignify can't target {', '.join(bad)} on Google yet.",
            "Use other countries, or add it in Google Ads after the campaign is made.", "geo.countries")
    if geo.get("zips") or geo.get("regions") or geo.get("cities"):
        add("block", "google.geo_detail", "On Google, Campaignify targets whole countries for now.",
            "Use country codes here, then narrow it to your area in Google Ads before you turn it on.", "geo")
    if (g.get("language") or "en") not in google.LANGUAGES:
        add("block", "google.language", "That language isn't one Campaignify sets on Google yet.", "Pick English or "
            "another language from the list.", "google.language")
    if "social_issues" in (spec.special_ad_categories or []):
        add("block", "google.political", "Political and election ads on Google need Google's advertiser verification.",
            "Make them in Google Ads after you are verified there.", "special_ad_categories")
    if set(spec.special_ad_categories or []) & {"housing", "credit", "employment"}:
        add("warn", "google.restricted_category", "Google limits targeting for housing, credit and job ads in the US "
            "and Canada.", "Check the campaign's settings in Google Ads before you turn it on.", "special_ad_categories")
    aud = spec.audience or {}
    if aud.get("age_min", 18) != 18 or aud.get("age_max", 65) != 65 or (aud.get("genders") or ["all"]) != ["all"]:
        add("warn", "google.audience_open", "On Google, Campaignify targets by location and "
            + ("keywords" if typ == "search" else "Google's own signals") + "; ages and gender are left open.",
            "Narrow them in Google Ads if you need to.", "audience")
    import conversion_actions
    if (typ == "pmax" or g.get("bidding") == "conversions") and not conversion_actions.ready(conn, conn_row):
        # cleared once the account tracks conversions: our lead / purchase actions (M3.5) or its own primary ones
        add("warn", "google.conversions_needed", "Google bids for the conversions you track in Google Ads.",
            "If you don't track any yet, set up conversion tracking in Settings first"
            + (", or pick Maximize clicks." if typ == "search" else "."), "google.bidding")
    if typ == "pmax":
        for i, pid in enumerate(g.get("logos") or []):
            a = assets.get(f"logo:{pid}")
            if a is None:
                add("block", "google.logo_unknown", f"Logo {i + 1} isn't one of this product's photos.", "Pick another.",
                    f"google.logos[{i}]")
            elif not a.get("field"):
                add("block", "google.logo_small", f"Logo {i + 1} is under 128 pixels wide, so Google won't take it.",
                    "Upload a larger logo to the product's photos.", f"google.logos[{i}]")
        for i, c in enumerate(spec.creatives):
            if c.kind != "image":
                add("block", "google.pmax_image_only", "Performance Max here uses images.", "Pick image ads.",
                    f"creatives[{i}]")
    return out


# ---------------------------------------------------------------- the approval card

def _quoted(items, n=6) -> str:
    shown = [f"[{k['term']}]" if k.get("match") == "exact" else f'"{k["term"]}"' if k.get("match") == "phrase"
             else k["term"] for k in items[:n]]
    more = len(items) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def make_card(conn, ws, spec, conn_row, steps: list, prov: dict, assets: dict) -> dict:
    cur, g = spec.currency, spec.google or {}
    typ = g.get("type") or "search"
    _, bid_words = google.bidding(spec)
    where = ", ".join((spec.geo or {}).get("countries") or []) or "no location"
    lang = google.LANGUAGE_WORDS.get(g.get("language") or "en", g.get("language") or "")
    money = launches.money(spec.daily_budget_minor, cur)
    lines = [f"1 campaign: {spec.name}. {TYPE_WORDS[typ]}, {money} a day, {bid_words}, PAUSED."]
    ups = [s for s in steps if s["kind"] == "upload"]
    if typ == "search":
        kws, negs = g.get("keywords") or [], g.get("negative_keywords") or []
        lines.append("Shows on Google Search" + (" and its search partners" if g.get("search_partners", True) else "")
                     + f" to people in {where}, searching in {lang}.")
        lines.append(f"1 ad group with {len(kws)} keyword{'s' if len(kws) != 1 else ''}: {_quoted(kws)}."
                     + (f" {len(negs)} negative keyword{'s' if len(negs) != 1 else ''}: {_quoted(negs, 4)}." if negs else ""))
        hs, ds = g.get("headlines") or [], g.get("descriptions") or []
        pins = [f'"{h}" in headline position {p}' for h, p in zip(hs, g.get("headline_pins") or []) if p] + \
               [f'"{d[:40]}" in description position {p}' for d, p in zip(ds, g.get("description_pins") or []) if p]
        lines.append(f"1 responsive search ad: {len(hs)} headlines, {len(ds)} descriptions, to {g.get('final_url') or 'no page'}."
                     + (" Pinned: " + "; ".join(pins) + "." if pins else ""))
        disclosure = "AI disclosure: a text ad, with no AI-made media."
        ai = False
    else:
        logos = [assets.get(f"logo:{p}") or {} for p in g.get("logos") or []]
        padded = [str(i + 1) for i, a in enumerate(logos) if a.get("padded")]
        lines.append(f"Brand: {g.get('business_name') or 'no business name'}, {len(logos)} logo{'s' if len(logos) != 1 else ''}"
                     + (f" (logo {', '.join(padded)} padded to a square on white)" if padded else "") + ".")
        lines.append(f"Shows across Google (Search, YouTube, Display, Gmail, Maps and Discover) to people in {where}, in {lang}.")
        imgs = len([s for s in ups if s["request"].get("asset_id")])
        lines.append(f"1 asset group: {len(g.get('headlines') or [])} headlines, {len(g.get('long_headlines') or [])} long "
                     f"headline{'s' if len(g.get('long_headlines') or []) != 1 else ''}, {len(g.get('descriptions') or [])} "
                     f"descriptions and {imgs} images (each picked ad as a centred landscape and square crop), to "
                     f"{(spec.creatives[0].destination_url if spec.creatives else '') or 'no page'}.")
        lines.append("Google won't write extra text from your site. With no video, Google can make one from these images "
                     "and text, and it can send people to other pages of your site. You can turn both off in Google Ads.")
        made = [i + 1 for i, c in enumerate(spec.creatives) if (prov.get(c.asset_id) or {}).get("ai_generated")]
        ai = bool(made)
        disclosure = (f"AI disclosure: {launches._ad_list(made).replace('ad', 'image', 1)} "
                      f"{'is' if len(made) == 1 else 'are'} AI-made media. Check Google's rules for AI images in your "
                      "category before you turn the campaign on.") if made else \
            "AI disclosure: none of these images are AI-made media."
    start, end = launches._day(spec.start_at), launches._day(spec.end_at)
    schedule = (f"Starts {start}" if start else "Starts when you turn it on") + (f", ends {end}." if end else ", no end date.")
    objs = [{"kind": s["kind"], "name": s["name"], "key": s["key"]} for s in steps if s["kind"] not in ("upload", "check")]
    account = (f"{conn_row['account_name'] or 'Google Ads account'} ({google.dashed(conn_row['external_account_id'])})"
               if conn_row is not None else "No Google Ads account connected")
    return {"platform": "google", "type": typ, "account": account,
            "page": (f"through manager {google.dashed(conn_row['business_id'])}" if conn_row is not None
                     and conn_row["business_id"] else ""),
            "lines": lines, "budget": f"{money} a day", "schedule": schedule, "disclosure": disclosure, "ai": ai,
            "objects": objs, "uploads": len(ups),
            "footer": "Nothing runs until you turn the campaign on. Inside it, the "
                      + ("ad group, keywords and ad" if typ == "search" else "asset group")
                      + " are on, so turning the campaign on is all it takes."}

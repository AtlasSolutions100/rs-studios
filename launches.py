"""Launch a plan as a PAUSED campaign on the member's own ad account (roadmap M2.3, design doc 4.2).

The promise: "launch by hand or let Campaignify create it". The flow:

    1. The Launch page (launch_routes.py) turns the plan into a CampaignSpec draft (draft()) that the
       member edits, and shows the approval card: exactly what will be made on Meta (campaign, ad set,
       ads, budget, schedule, AI disclosure), with the validator's and the policy linter's findings
       (adpolicy.preflight, as it stands) plus this file's launch rules (launch_issues()). A "block"
       finding keeps the button off.
    2. An owner approves the card (approve()). That stores one `launches` row per idempotency key
       (sha256 of workspace, plan and spec hash: approving the same card twice gives the same launch)
       and its ledger in `launch_steps`, straight from the adapter's plan_steps(): the same steps the
       card was written from, so the card and what gets made can't drift apart.
    3. The worker runs the saga (tick() -> run()): each step in order (uploads, creatives, the campaign,
       the ad set, the ads), every object PAUSED, each written to `launch_steps` and `ad_objects` as it
       lands. Every object's name ends in "[cfy:<launch id>:<step>]": a step that may have run before
       (a timeout, a crash, a retry) is looked up on Meta first, so a re-run creates nothing new.
       Rate limits and a video still processing don't fail the launch: the step waits (next_at).
    4. A step that fails for good (after up to STEP_ATTEMPTS tries for errors worth retrying) stops the
       launch and compensates in reverse: ads and creatives are deleted; the campaign and ad set stay
       PAUSED and are reported (deleting a campaign on Meta is final); uploads stay in the account's
       library. Status rolled_back when that all worked, failed when something couldn't be removed
       (the page lists it, with Ads Manager links). "Try again" re-runs the same launch, reusing what
       was kept.

Google (M3.2 Search, M3.3 Performance Max; launch_google.py has its draft, rules and card) runs through the
same card, approval, ledger, saga and compensation, behind GOOGLE_ADS as well: one mutate makes the whole
campaign (Google applies all of it or none), then a GAQL check reads it back. Its daily budget follows the
same D3 ceilings. Snapchat (M6.1; launch_video.py has its draft, rules and card, adplatforms/snapchat.py the
steps) does too, behind SNAPCHAT_ADS: uploads, creatives, the campaign, the ad squad, the ads, all PAUSED. So does
TikTok (M6.2, adplatforms/tiktok.py, behind TIKTOK_ADS): uploads, the campaign (off), the ad group, the ads.

Rules (Koby's decisions): launching needs a paid plan (D2); a campaign's daily budget is capped per plan
(D3: Starter 200, Growth 1,000, Scale 5,000 a day, in the ad account's currency; "ask us" above); every
ad must come from the workspace's library so its AI provenance is known (re-audit I-7). Everything sits
behind AD_LAUNCH (adplatforms.enabled; default off) and its AD_LAUNCH_WORKSPACES allowlist; with the
flag off the worker starts nothing (queued launches wait; objects already made are PAUSED).
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import adhttp
import adplatforms
import adpolicy
import adspec
import db
import plans
import secretsbox
from adspec import Issue

log = logging.getLogger("campaignify.launches")

# D3: the daily budget a plan can create, in US dollars; converted to the ad account's currency with
# UNITS_PER_USD so the limit is worth about the same everywhere (Koby, Nov 8). It is a safety net, not
# billing, so approximate rates are fine; a currency not listed keeps the same number.
CEILINGS_USD = {"starter": 200, "growth": 1000, "scale": 5000}
UNITS_PER_USD = {  # approximate, Nov 2026; refresh now and then
    "USD": 1, "EUR": 0.92, "GBP": 0.78, "CAD": 1.38, "AUD": 1.52, "NZD": 1.68, "CHF": 0.88, "SEK": 10.6,
    "NOK": 10.8, "DKK": 6.9, "PLN": 3.9, "CZK": 23, "HUF": 370, "RON": 4.6, "ISK": 137, "MXN": 18.5,
    "BRL": 5.4, "CLP": 950, "COP": 4100, "PEN": 3.7, "CRC": 510, "PYG": 7800, "INR": 84, "JPY": 150,
    "KRW": 1380, "TWD": 32, "HKD": 7.8, "SGD": 1.34, "MYR": 4.5, "THB": 34, "PHP": 58, "IDR": 16000,
    "VND": 25000, "ZAR": 18, "AED": 3.67, "SAR": 3.75, "QAR": 3.64, "ILS": 3.7, "PKR": 280, "KES": 129,
}
SUPPORT_EMAIL = "hello@campaignify.ai"
DEFAULT_DAILY = 2000              # 20.00 a day when the plan has no budget in money
STEP_ATTEMPTS = 3                 # tries per step for errors worth retrying (network, Meta 5xx / transient)
RETRY_GAP_S = 10.0
LEASE_S = 300                     # a run that died holds its launch this long
WAIT_MAX_S = 2 * 3600             # a launch that keeps waiting (rate limits, video processing) longer fails
COMP_WAIT_S = 90.0                # compensation waits this long for Meta's budget or edit slot
TICK_SECONDS = 5
CLOCK = time.time                 # tests move it
_last_tick = [0.0]
ACTIVE = ("queued", "launching")
FINISHED = ("paused", "failed", "rolled_back")

KIND_WORDS = {"upload": "Upload", "creative": "Ad creative", "campaign": "Campaign", "adset": "Ad set", "ad": "Ad",
              "adgroup": "Ad group", "assetgroup": "Asset group", "check": "Check", "adsquad": "Ad squad"}
PLATFORM_WORDS = {"meta": "Meta", "google": "Google", "snapchat": "Snapchat", "tiktok": "TikTok"}
ADSET_KINDS = ("adset", "adsquad", "adgroup")       # the ad set of a launch, by platform (its step key is "adset" on each)
OBJECTIVE_WORDS = {"sales": "Sales", "leads": "Leads", "traffic": "Traffic", "awareness": "Awareness"}
GOAL_TO_OBJECTIVE = {"sales": "sales", "leads": "leads", "traffic": "traffic", "awareness": "awareness",
                     "engagement": "traffic", "installs": "traffic"}
CTA_BY_OBJECTIVE = {"sales": "SHOP_NOW", "leads": "LEARN_MORE", "traffic": "LEARN_MORE", "awareness": "LEARN_MORE"}
CTA_WORDS = {"SHOP_NOW": "Shop now", "LEARN_MORE": "Learn more", "SIGN_UP": "Sign up", "BOOK_NOW": "Book now",
             "CONTACT_US": "Contact us", "GET_QUOTE": "Get quote", "ORDER_NOW": "Order now", "SUBSCRIBE": "Subscribe",
             "APPLY_NOW": "Apply now", "DOWNLOAD": "Download", "GET_OFFER": "Get offer", "CALL_NOW": "Call now"}
VERTICAL_WORDS = {"general": "Something else", "health": "Health or wellness", "supplements": "Supplements",
                  "weight_loss": "Weight loss", "cosmetics": "Skin care or cosmetics", "finance": "Money or investing",
                  "crypto": "Crypto", "alcohol": "Alcohol", "housing": "Housing or real estate",
                  "credit": "Loans or credit", "employment": "Jobs or hiring"}
UTM = "utm_source=facebook&utm_medium=paid&utm_campaign={{campaign.id}}&utm_content={{ad.id}}"

# Meta errors we can put in plain words: (code, subcode or None) -> message. Anything else shows Meta's own
# user message (error_user_title / error_user_msg), which Meta writes for the person creating the ad.
ERRORS = {
    (190, None): "Meta ended Campaignify's access to this ad account. Reconnect it in Settings > Ad accounts, "
                 "then try again.",
    (102, None): "Meta ended Campaignify's access to this ad account. Reconnect it in Settings > Ad accounts, "
                 "then try again.",
    (10, None): "Campaignify is missing a Meta permission for this. Reconnect the ad account and allow every "
                "permission Meta asks about.",
    (200, None): "Campaignify is missing a Meta permission for this. Reconnect the ad account and allow every "
                 "permission Meta asks about.",
    (294, None): "Meta needs ads_management for this. Reconnect the ad account and allow it.",
    (100, 1885183): "Meta won't create ads through an app that is still in development mode. The Campaignify "
                    "Meta app has to be switched to Live first.",
    (368, None): "Meta blocked this as against its advertising policies. Check the ad copy and the landing page.",
    (2635, None): "Meta refused the API version Campaignify uses. Email us and we'll fix it.",
    (613, 4841018): "Meta allows one change per item every 30 seconds. Try again in a minute.",
}
NETWORK_WORDS = "We couldn't reach Meta. Try again in a few minutes."
WAITED_WORDS = "Meta kept asking us to slow down for over 2 hours. Try again later."


# ---------------------------------------------------------------- small helpers

def _now_iso(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(CLOCK() + offset_s, timezone.utc).replace(microsecond=0).isoformat()


def _age_s(iso: str) -> float:
    try:
        return CLOCK() - datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def on(ws, user=None) -> bool:
    """The AD_LAUNCH flag for this workspace (off by default; beta = AD_LAUNCH_WORKSPACES and admins)."""
    return adplatforms.enabled(ws, user)


def money(minor: int, currency: str) -> str:
    import campaign_budget
    f = adspec.minor_factor(currency)
    return campaign_budget.money(minor / f, currency) if currency in campaign_budget.CURRENCIES \
        else f"{minor / f:,.{2 if f == 100 else 0}f} {currency}"


def _two_sig(x: float) -> float:
    """Round to two significant figures (184.0 -> 180, 3700 -> 3700, 150000 -> 150000)."""
    if x <= 0:
        return 0
    from math import floor, log10
    p = 10 ** (floor(log10(x)) - 1)
    return round(x / p) * p


def paid_plan_code(ws) -> str:
    """D2: the plan the workspace pays for. A comp (comp_plan) or an operator's plan_override is not a
    payment, so it doesn't unlock launching (Koby, Nov 8); a subscription does, discount codes included."""
    if ws is None:
        return "free"
    d = {k: ws[k] for k in ws.keys()}
    d["plan_override"], d["comp_plan"] = None, None
    return plans.plan_code(d)


def launch_plan_code(ws, operator: bool = False) -> str:
    """The plan launching goes by: the paid plan; an operator (admin) testing in a comped workspace gets
    the workspace's plan in force so the beta can be tried before anyone pays."""
    code = paid_plan_code(ws)
    if code == "free" and operator:
        code = plans.plan_code(ws)
    return code


def ceiling(ws, currency: str | None = "USD", operator: bool = False) -> int | None:
    """The plan's daily budget limit in minor units of `currency` (None when the plan can't launch)."""
    usd = CEILINGS_USD.get(launch_plan_code(ws, operator))
    if not usd:
        return None
    cur = (currency or "USD").upper()
    rate = UNITS_PER_USD.get(cur)
    amount = _two_sig(usd * rate) if rate else usd
    return int(round(amount * adspec.minor_factor(cur)))


def connection(conn, ws_id: int, platform: str = "meta"):
    """The workspace's active connection on `platform` (the newest), or None."""
    return conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND platform=? AND status='active' "
                        "ORDER BY updated_at DESC, id DESC LIMIT 1", (int(ws_id), platform)).fetchone()


def pixels_of(conn, ws_id: int, conn_id: int) -> list:
    return conn.execute("SELECT * FROM pixels WHERE workspace_id=? AND connection_id=? ORDER BY id",
                        (int(ws_id), int(conn_id))).fetchall()


def _aspect(path: str) -> str:
    """The nearest spec aspect of an image (or a video's poster)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        return "4:5"
    r = w / h if h else 0.8
    return min((("9:16", 9 / 16), ("4:5", 0.8), ("1:1", 1.0), ("16:9", 16 / 9), ("1.91:1", 1.91)),
               key=lambda a: abs(a[1] - r))[0]


def assets_for(conn, ws_id: int, asset_ids) -> dict:
    """{outputs.id: {"kind", "path", "poster", "preview", "tool"}} for this workspace's library items."""
    ids = [int(a) for a in asset_ids or [] if str(a).isdigit()]
    if not ids:
        return {}
    rows = conn.execute(f"SELECT o.id, o.kind, o.path, j.tool, j.params_json, j.status, j.deleted_at FROM outputs o "
                        f"JOIN jobs j ON j.id=o.job_id WHERE j.workspace_id=? AND o.id IN ({','.join('?' * len(ids))})",
                        (int(ws_id), *ids)).fetchall()
    out = {}
    for r in rows:
        try:
            preview = bool(json.loads(r["params_json"] or "{}").get("preview"))
        except ValueError:
            preview = False
        poster = (r["path"] or "") + ".jpg" if r["kind"] == "video" else None
        out[r["id"]] = {"kind": r["kind"], "path": r["path"], "poster": poster, "tool": r["tool"],
                        "preview": preview, "gone": bool(r["deleted_at"]) or r["status"] != "done"}
    return out


# ---------------------------------------------------------------- the draft spec, from the plan

def plan_ads(conn, ws_id: int, cid: int) -> list:
    """The plan's finished Meta ads: [{"asset_id", "kind", "angle", "path"}], library ids only."""
    rows = conn.execute("SELECT a.angle, o.id AS oid, o.kind, o.path FROM campaign_ads a JOIN jobs j ON j.id=a.job_id "
                        "JOIN outputs o ON o.job_id=j.id WHERE a.campaign_id=? AND a.workspace_id=? AND "
                        "a.platform='meta' AND j.status='done' AND j.deleted_at IS NULL ORDER BY a.id, o.id",
                        (int(cid), int(ws_id))).fetchall()
    return [{"asset_id": r["oid"], "kind": r["kind"] if r["kind"] in adspec.KINDS else "image",
             "angle": r["angle"] or "", "path": r["path"]} for r in rows]


def _copy_for(plan: dict, angle: str) -> dict:
    """{"primary_text", "headline", "description"} from the plan's Meta copy: the variant for this angle, else
    the first one that passed the checks."""
    variants = [v for cp in plan.get("copy") or [] if cp.get("platform") == "meta" for v in cp.get("variants") or []]
    v = next((x for x in variants if x.get("angle") == angle and x.get("ok")), None) or \
        next((x for x in variants if x.get("ok")), None) or (variants[0] if variants else None)
    out = {"primary_text": "", "headline": "", "description": ""}
    for f in (v or {}).get("fields") or []:
        n = (f.get("name") or "").lower()
        key = "primary_text" if "primary" in n or n in ("text", "caption", "body") else \
            "headline" if "headline" in n or "title" in n else "description" if "description" in n else None
        if key and not out[key]:
            out[key] = (f.get("text") or "").strip()
    return out


def _daily_from_brief(brief: dict, plan: dict, currency: str = "USD", platform: str = "meta") -> int:
    import campaign_budget
    if not campaign_budget.structured(brief):
        setup = (plan.get("final") or {}).get("setup") or {}
        row = next((r for r in setup.get("rows") or [] if r.get("platform") == platform), None)
        if row and setup.get("currency") == (currency or "USD").upper() and row.get("mid"):
            return max(adspec.MIN_DAILY[platform], int(row["mid"]))     # Planner v2's suggested amount (M4.3)
        return DEFAULT_DAILY
    src = (brief.get("budget_currency") or "USD").upper()
    cur = (currency or "USD").upper()
    share = next((b.get("share_percent") for b in (plan.get("final") or {}).get("budget_split") or []
                  if b.get("platform") == platform), 100)
    try:
        share = float(share)
    except (TypeError, ValueError):
        share = 100.0
    amount = float(brief["budget_amount"]) * share / 100
    if brief.get("budget_period") != "day":
        amount /= max(1, int(brief.get("budget_days") or campaign_budget.DEFAULT_DAYS))
    if src != cur and src in UNITS_PER_USD and cur in UNITS_PER_USD:
        amount = _two_sig(amount / UNITS_PER_USD[src] * UNITS_PER_USD[cur])   # the brief's currency to the account's (D3)
    return max(adspec.MIN_DAILY[platform], int(round(amount)) * adspec.minor_factor(currency))


def _local_today(tz_name: str | None) -> date:
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.fromtimestamp(CLOCK(), tz).date()


def draft(conn, ws, c, conn_row) -> dict:
    """A CampaignSpec (as a dict) for this plan on this Meta connection, before the member's edits."""
    brief, plan = json.loads(c["brief_json"] or "{}"), json.loads(c["plan_json"] or "{}")
    objective = GOAL_TO_OBJECTIVE.get((brief.get("goal") or "sales").lower(), "sales")
    link = (brief.get("link") or "").strip()
    if not link.startswith("https://"):
        link = ""
    pxs = pixels_of(conn, ws["id"], conn_row["id"]) if conn_row else []
    start = _local_today(conn_row["timezone"] if conn_row else None) + timedelta(days=1)
    import campaign_budget
    days = int(brief.get("budget_days") or 0) if campaign_budget.structured(brief) else 0
    creatives = []
    for i, a in enumerate(plan_ads(conn, ws["id"], c["id"])):
        cp = _copy_for(plan, a["angle"])
        creatives.append({"kind": a["kind"], "asset_id": a["asset_id"], "name": (a["angle"] or f"Ad {i + 1}")[:80],
                          "primary_text": cp["primary_text"][:2000], "headline": cp["headline"][:255],
                          "description": cp["description"][:255], "cta": CTA_BY_OBJECTIVE[objective],
                          "destination_url": link, "utm": UTM,
                          "aspect": _aspect((a["path"] + ".jpg") if a["kind"] == "video" else a["path"])})
    d = {"objective": objective, "platform": "meta", "name": (c["name"] or "Campaignify campaign")[:120],
            "daily_budget_minor": _daily_from_brief(brief, plan, (conn_row["currency"] if conn_row and conn_row["currency"]
                                                                  else "USD")),
            "currency": (conn_row["currency"] if conn_row and conn_row["currency"] else "USD"),
            "start_at": start.isoformat(), "end_at": (start + timedelta(days=days)).isoformat() if days else None,
            "geo": {"countries": ["US"]}, "audience": {"age_min": 18, "age_max": 65, "genders": ["all"]},
            "placements": "auto",
            "conversion": ({"pixel_id": pxs[0]["external_pixel_id"],
                            "event": "Purchase" if objective == "sales" else "Lead", "landing_url": link}
                           if pxs and objective in ("sales", "leads") else None),
            "creatives": creatives, "special_ad_categories": [], "ai_disclosure": False, "google": None,
            "vertical": "general", "claims_substantiated": False}
    # Prefill what is advertised from the product and the copy (adpolicy.derive_vertical), so a beauty or supplement
    # product starts on its own rules instead of hitting policy.vertical_mismatch; the member can still change it.
    spec, _ = adspec.from_dict(d)
    if spec is not None and spec.creatives:
        prov = adpolicy.provenance(conn, ws["id"], [x.asset_id for x in spec.creatives if x.asset_id])
        vertical, _why = adpolicy.derive_vertical(spec, prov)
        if vertical in adspec.VERTICALS:
            d["vertical"] = vertical
            if vertical in ("housing", "credit", "employment"):
                d["special_ad_categories"] = [vertical]
    return d


def apply_form(base: dict, form, all_ads: list) -> dict:
    """The member's edits on the Launch page on top of `base` (a spec dict). Unknown values are left for the
    shape check (adspec.from_dict) to refuse."""
    d = json.loads(json.dumps(base))
    g = form.get
    if g("name") is not None:
        d["name"] = (g("name") or "").strip()[:200]
    if g("objective") in adspec.OBJECTIVES:
        d["objective"] = g("objective")
    if g("daily_budget") is not None:
        try:
            d["daily_budget_minor"] = int(round(float((g("daily_budget") or "0").replace(",", ""))
                                                * adspec.minor_factor(d.get("currency"))))
        except ValueError:
            d["daily_budget_minor"] = 0
    for k, f in (("start_at", "start"), ("end_at", "end")):
        if g(f) is not None:
            v = (g(f) or "").strip()
            d[k] = v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else (None if not v else v)
    if g("countries") is not None:
        d["geo"] = {"countries": [c.strip().upper() for c in re.split(r"[,\s]+", g("countries") or "") if c.strip()][:25]}
    aud = d.setdefault("audience", {})
    for k in ("age_min", "age_max"):
        if g(k) is not None:
            try:
                aud[k] = int(g(k))
            except ValueError:
                aud[k] = -1
    if g("genders") in ("all", "women", "men"):
        aud["genders"] = [g("genders")]
    if g("vertical") in adspec.VERTICALS:
        d["vertical"] = g("vertical")
    if g("form") == "1" and g("placement_mode") in ("auto", "choose"):
        # Meta (M6.3): automatic, or the ticked apps and spots (Instagram Feed, Stories, Reels; Facebook's too)
        keys = dict(adspec.META_CHOICES)
        d["placements"] = "auto" if g("placement_mode") == "auto" else \
            [k for k in keys if k in form.getlist("place")]
    if g("form") == "1":
        d["special_ad_categories"] = [s for s in form.getlist("special") if s in adspec.SPECIAL_AD_CATEGORIES]
        d["claims_substantiated"] = g("claims") == "1"
    landing = (g("landing") or "").strip() if g("landing") is not None else None
    if d["objective"] in ("sales", "leads"):
        px = g("pixel")
        if px is not None:
            d["conversion"] = {"pixel_id": px, "event": g("event") or ("Purchase" if d["objective"] == "sales" else "Lead"),
                               "landing_url": landing if landing is not None else (d.get("conversion") or {}).get("landing_url", "")} \
                if px else None
    else:
        d["conversion"] = None
    if g("form") == "1":
        by_id = {int(a["asset_id"]): a for a in all_ads}
        keep = []
        for c in d["creatives"]:
            if c.get("asset_id") in by_id:
                by_id.pop(c["asset_id"])
            keep.append(c)
        # ads of the plan that the draft didn't have yet (picked again after being left out)
        for a in by_id.values():
            keep.append({"kind": a["kind"], "asset_id": a["asset_id"], "name": a["angle"][:80] or "Ad", "cta": "LEARN_MORE",
                         "destination_url": "", "utm": UTM, "aspect": _aspect((a["path"] + ".jpg") if a["kind"] == "video"
                                                                              else a["path"])})
        picked = []
        for c in keep:
            n = str(c.get("asset_id"))
            if g(f"use_{n}") != "1":
                continue
            for fld, key, lim in (("primary_text", "text", 2200), ("headline", "head", 255), ("description", "desc", 255)):
                if g(f"{key}_{n}") is not None:
                    c[fld] = (g(f"{key}_{n}") or "").strip()[:lim]
            if g(f"cta_{n}") in adspec.CTAS:
                c["cta"] = g(f"cta_{n}")
            if landing is not None:
                c["destination_url"] = landing
            picked.append(c)
        d["creatives"] = picked
    return d


# ---------------------------------------------------------------- review: issues and the approval card

def launch_issues(conn, ws, spec, conn_row, assets: dict, prov: dict, operator: bool = False) -> list:
    """The launch rules on top of adpolicy.preflight: plan (D2), budget ceiling (D3), the connection, library
    assets only (I-7), the pixel."""
    out = []
    add = lambda *a: out.append(Issue(*a))  # noqa: E731
    word = PLATFORM_WORDS.get(spec.platform, spec.platform)
    code = launch_plan_code(ws, operator)
    if code == "free":
        add("block", "launch.paid_plan", "Creating campaigns from Campaignify needs an active subscription.",
            "Pick a plan on the Billing page. You can still launch by hand with the launch kit.", "plan")
    import billing_holds
    if billing_holds.reason(ws):
        add("block", "launch.billing_hold", "This workspace is on hold, so nothing new can be launched.",
            "See the banner at the top of the page.", "plan")
    cap = ceiling(ws, spec.currency, operator)
    if cap and spec.daily_budget_minor > cap:
        add("block", "launch.budget_ceiling",
            f"Your {plans.PLANS[code]['name']} plan creates campaigns up to {money(cap, spec.currency)} a day.",
            f"Lower the daily budget, or email {SUPPORT_EMAIL} to raise the limit.", "daily_budget_minor")
    if conn_row is None or conn_row["status"] != "active" or not conn_row["token_enc"]:
        add("block", "launch.no_connection", f"No {word} ad account is connected, or it needs Reconnect.",
            "Connect it in Settings > Ad accounts.", "connection")
    elif conn_row["platform"] != spec.platform:
        add("block", "launch.wrong_platform", f"That connection isn't a {word} account.", "Reload the page.", "connection")
    elif spec.platform == "google":
        import launch_google
        if spec.currency != (conn_row["currency"] or spec.currency):
            add("block", "launch.currency", f"The account bills in {conn_row['currency']}, not {spec.currency}.",
                "Reload the page.", "currency")
        out += launch_google.issues(conn, ws, spec, conn_row, assets)
    elif spec.platform in _video_platforms():
        import launch_video
        out += launch_video.issues(conn, ws, spec, conn_row, assets)
    else:
        if not conn_row["page_id"]:
            add("block", "launch.no_page", "Ads on Meta run from a Facebook Page, and none is picked.",
                "Reconnect the ad account and pick your Page.", "connection")
        # M6.3: Instagram placements run as the Page's Instagram account (instagram_user_id on each creative).
        ig = isinstance(spec.placements, list) and set(spec.placements) & adspec.INSTAGRAM_PLACEMENTS
        if ig and not conn_row["instagram_actor_id"]:
            add("block", "launch.no_instagram", "Instagram placements need the Instagram account linked to your Page, "
                "and none is picked.", "Link it to the Page on Meta and reconnect, or untick the Instagram placements.",
                "placements")
        elif spec.placements == "auto" and not conn_row["instagram_actor_id"]:
            add("warn", "launch.auto_no_instagram", "Automatic placements include Instagram, and no Instagram account is "
                "linked to the Page.", "Link one on Meta and reconnect so Instagram shows your own account, or pick "
                "Facebook placements only.", "placements")
        scopes = set((conn_row["scopes"] or "").split())
        miss = [s for s in ("ads_management", "pages_manage_ads") if scopes and s not in scopes]
        if miss:
            add("block", "launch.permission", f"Campaignify doesn't have Meta's {' and '.join(miss)} permission.",
                "Reconnect the ad account and allow every permission Meta asks about.", "connection")
        if spec.currency != (conn_row["currency"] or spec.currency):
            add("block", "launch.currency", f"The ad account bills in {conn_row['currency']}, not {spec.currency}.",
                "Reload the page.", "currency")
        px = (spec.conversion or {}).get("pixel_id")
        if px and px not in {p["external_pixel_id"] for p in pixels_of(conn, ws["id"], conn_row["id"])}:
            add("block", "launch.pixel_unknown", "That pixel isn't one of this ad account's pixels.",
                "Pick a pixel from the list.", "conversion.pixel_id")
    for i, c in enumerate(spec.creatives):
        w = f"creatives[{i}]"
        a = assets.get(c.asset_id) if c.asset_id else None
        if not c.asset_id:
            # I-7: provenance comes from the library; an ad from elsewhere would leave the AI disclosure unknown.
            add("block", "launch.no_asset", "Each ad has to come from your library, so we know whether it needs "
                "the AI label.", "Pick a finished ad from this plan.", w)
        elif a is None or c.asset_id not in prov:
            add("block", "launch.asset_unknown", "That ad isn't in this workspace's library.", "Pick another ad.", w)
        elif a["gone"]:
            add("block", "launch.asset_gone", "That ad was deleted or never finished.", "Pick another ad.", w)
        elif a["preview"]:
            add("block", "launch.asset_preview", "That ad is a watermarked preview.", "Make the full ad first.", w)
        elif not a["path"] or not os.path.exists(a["path"]):
            add("block", "launch.asset_file", "We can't find that ad's file.", "Make it again or pick another ad.", w)
        elif a["kind"] != c.kind:
            add("block", "launch.asset_kind", f"That ad is a {a['kind']}, not a {c.kind}.", "Reload the page.", w)
    return out


def review(conn, ws, spec_dict: dict, conn_row, operator: bool = False) -> tuple:
    """(spec or None, issues, card, steps) for a spec dict. The spec's AI flags are forced from the library
    (adpolicy.apply_provenance). steps are the adapter's exact calls; the card is written from them."""
    spec, issues = adspec.from_dict(spec_dict)
    if spec is None:
        return None, issues, None, []
    issues = adpolicy.preflight(spec, conn=conn, workspace_id=ws["id"])
    ids = [c.asset_id for c in spec.creatives if c.asset_id]
    prov = adpolicy.provenance(conn, ws["id"], ids)
    assets = assets_for(conn, ws["id"], ids)
    if spec.platform == "google":
        import launch_google
        assets.update(launch_google.logo_assets(conn, ws["id"], spec))
    issues += launch_issues(conn, ws, spec, conn_row, assets, prov, operator)
    ok_row = conn_row is not None and conn_row["platform"] == spec.platform
    steps = adplatforms.adapter(spec.platform).plan_steps(spec, conn_row, assets) if ok_row else []
    if spec.platform == "google":
        return spec, issues, launch_google.make_card(conn, ws, spec, conn_row, steps, prov, assets), steps
    if spec.platform in _video_platforms():
        import launch_video
        return spec, issues, launch_video.make_card(conn, ws, spec, conn_row, steps, prov), steps
    return spec, issues, make_card(conn, ws, spec, conn_row, steps, prov), steps


def _video_platforms() -> tuple:
    import launch_video
    return launch_video.PLATFORMS


def _day(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    return d.strftime("%a %b ") + str(d.day) + d.strftime(", %Y")


def make_card(conn, ws, spec, conn_row, steps: list, prov: dict) -> dict:
    """The approval card: what Campaignify will create, in plain words, from the exact steps it will run."""
    cur = spec.currency
    objs = [{"kind": s["kind"], "name": s["name"], "key": s["key"]} for s in steps if s["kind"] != "upload"]
    ads = [s for s in steps if s["kind"] == "ad"]
    aud, geo = spec.audience or {}, spec.geo or {}
    genders = {"women": "women", "men": "men"}.get((aud.get("genders") or ["all"])[0], "all genders")
    where = ", ".join(geo.get("countries") or []) or "no location"
    words = dict(adspec.META_CHOICES)
    place = "automatic placements" if spec.placements == "auto" else \
        "placements: " + ", ".join(words.get(p, p.replace("_", " ")) for p in spec.placements)
    px_name = None
    conv = spec.conversion or {}
    if conn_row is not None and conv.get("pixel_id"):
        row = next((p for p in pixels_of(conn, ws["id"], conn_row["id"]) if p["external_pixel_id"] == conv["pixel_id"]), None)
        px_name = (row["name"] if row else None) or f"pixel {conv['pixel_id']}"
    goal = f"optimizes for {conv.get('event') or 'Purchase'} on {px_name}" if px_name and spec.objective in ("sales", "leads") \
        else {"traffic": "optimizes for link clicks", "awareness": "optimizes for reach"}.get(spec.objective, "")
    start, end = _day(spec.start_at), _day(spec.end_at)
    schedule = (f"Starts {start}" if start else "Starts when you turn it on") + (f", ends {end}." if end else ", no end date.")
    presenter = [i + 1 for i, c in enumerate(spec.creatives) if c.ai_presenter or c.synthetic_voice]
    made = [i + 1 for i, c in enumerate(spec.creatives) if i + 1 not in presenter
            and (prov.get(c.asset_id) or {}).get("ai_generated")]
    ai_ads = sorted(presenter + made)
    lines = [f"1 campaign: {spec.name}. {OBJECTIVE_WORDS[spec.objective]}, {money(spec.daily_budget_minor, cur)} a day, PAUSED.",
             f"1 ad set: {where}, ages {aud.get('age_min', 18)} to {aud.get('age_max', 65)}, {genders}, {place}"
             + (f", {goal}" if goal else "") + ". PAUSED.",
             f"{len(ads)} ad{'s' if len(ads) != 1 else ''}, PAUSED: " + "; ".join(s["name"] for s in ads) + "."]
    if spec.special_ad_categories:
        lines.append("Special ad category: " + ", ".join(spec.special_ad_categories) + ".")
    if ai_ads:
        parts = []
        if presenter:
            parts.append(f"{_ad_list(presenter)} {'has' if len(presenter) == 1 else 'have'} an AI presenter or AI voice")
        if made:
            parts.append(f"{_ad_list(made)} {'is' if len(made) == 1 else 'are'} AI-made media")
        disclosure = ("AI disclosure: " + "; ".join(parts) + ". Before you turn the campaign on, tick Meta's AI label on "
                      + ("that ad" if len(ai_ads) == 1 else "those ads") + " in Ads Manager.")
    else:
        disclosure = "AI disclosure: none of these ads use AI-made media."
    account = (f"{conn_row['account_name'] or 'Ad account'} (act_{conn_row['external_account_id']})"
               if conn_row is not None else "No ad account connected")
    page = (conn_row["page_name"] or "") if conn_row is not None else ""
    if conn_row is not None and conn_row["instagram_username"]:
        page += f" and Instagram @{conn_row['instagram_username']}"
    return {"platform": "meta", "account": account, "page": page or "No Page picked", "lines": lines,
            "budget": f"{money(spec.daily_budget_minor, cur)} a day", "schedule": schedule,
            "disclosure": disclosure, "ai": bool(ai_ads), "objects": objs,
            "uploads": sum(1 for s in steps if s["kind"] == "upload"),
            "footer": "Nothing runs until you turn it on."}


def _ad_list(nums: list) -> str:
    """"ad 2", "ads 1 and 2", "ads 1, 2 and 3"."""
    n = [str(x) for x in nums]
    return f"ad {n[0]}" if len(n) == 1 else "ads " + ", ".join(n[:-1]) + " and " + n[-1]


def card_text(card: dict) -> str:
    """The card as one block of text (the page renders the same parts)."""
    word = PLATFORM_WORDS.get(card.get("platform") or "meta", "Meta")
    return "\n".join([f"What Campaignify will create on {word}, in {card['account']}:"] + card["lines"]
                     + [card["schedule"], card["disclosure"], card["footer"]])


def save_card(conn, ws_id: int, user_id: int, cid: int, conn_row, spec, card: dict, issues: list) -> int:
    d = adspec.to_dict(spec)
    h = adspec.spec_hash(spec)
    after = dict(card, issues=[i.as_dict() for i in issues], blocking=adpolicy.blocking(issues))
    row = conn.execute("SELECT id FROM approval_cards WHERE workspace_id=? AND campaign_id=? AND kind='launch' AND "
                       "spec_hash=? AND decision IS NULL ORDER BY id DESC LIMIT 1", (ws_id, cid, h)).fetchone()
    if row:
        conn.execute("UPDATE approval_cards SET after_json=?, connection_id=? WHERE id=?",
                     (json.dumps(after), conn_row["id"] if conn_row is not None else None, row["id"]))
        conn.commit()
        return row["id"]
    card_id = conn.execute("INSERT INTO approval_cards (workspace_id, kind, campaign_id, connection_id, spec_json, spec_hash, "
                           "before_json, after_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (ws_id, "launch", cid, conn_row["id"] if conn_row is not None else None, json.dumps(d), h,
                            None, json.dumps(after), user_id, db.now())).lastrowid
    conn.commit()
    return card_id


def idempotency_key(ws_id: int, plan_id: int, spec_hash: str) -> str:
    return hashlib.sha256(f"{int(ws_id)}:{int(plan_id or 0)}:{spec_hash}".encode()).hexdigest()


class NotApproved(Exception):
    """The card can't be approved; str() is the reason in plain words."""


def approve(conn, ws, user, card_row) -> tuple:
    """Approve a launch card: (launch_id, new?). Re-runs the review on the stored spec (the connection, the plan,
    the pixels and the library may have changed since the card was drawn) and refuses on any block. The same
    spec for the same plan is one launch (the idempotency key): approving it again returns that launch, and
    re-queues it when it had failed."""
    if card_row["kind"] != "launch":
        raise NotApproved("That isn't a launch card.")
    conn_row = conn.execute("SELECT * FROM ad_connections WHERE id=? AND workspace_id=?",
                            (card_row["connection_id"] or 0, ws["id"])).fetchone()
    spec, issues, card, steps = review(conn, ws, json.loads(card_row["spec_json"]), conn_row)
    if spec is None or adpolicy.blocking(issues):
        first = next((i for i in issues if i.level == "block"), None)
        raise NotApproved(first.message if first else "The card has problems to fix first.")
    if adspec.spec_hash(spec) != card_row["spec_hash"]:
        raise NotApproved("The ads changed since this card was made. Update the card and check it again.")
    key = idempotency_key(ws["id"], card_row["campaign_id"], card_row["spec_hash"])
    now = _now_iso()
    existing = conn.execute("SELECT * FROM launches WHERE idempotency_key=?", (key,)).fetchone()
    if existing:
        if existing["workspace_id"] != ws["id"]:
            raise NotApproved("That card belongs to another workspace.")
        if existing["status"] in ("failed", "rolled_back"):
            requeue(conn, existing["id"])
        return existing["id"], False
    cur = conn.execute("INSERT OR IGNORE INTO launches (workspace_id, plan_id, platform, connection_id, card_id, spec_json, "
                       "status, idempotency_key, created_by, approved_by, approved_at, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ws["id"], card_row["campaign_id"], spec.platform, conn_row["id"], card_row["id"],
                        json.dumps(adspec.to_dict(spec)), "queued", key, card_row["created_by"], user["id"], now, now, now))
    if not cur.rowcount:          # a double click that lost the race: the other request made it
        conn.rollback()
        return conn.execute("SELECT id FROM launches WHERE idempotency_key=?", (key,)).fetchone()["id"], False
    lid = cur.lastrowid
    for n, s in enumerate(steps, start=1):
        conn.execute("INSERT INTO launch_steps (launch_id, step_no, step_key, kind, name, request_json, status) "
                     "VALUES (?,?,?,?,?,?,?)", (lid, n, s["key"], s["kind"], s["name"], json.dumps(s["request"]), "pending"))
    conn.execute("UPDATE approval_cards SET decision='approved', decided_by=?, decided_at=?, launch_id=?, after_json=? "
                 "WHERE id=?", (user["id"], now, lid, json.dumps(dict(card, issues=[i.as_dict() for i in issues],
                                                                         blocking=False)), card_row["id"]))
    conn.commit()
    log.info("launch %s approved by user %s: %s steps", lid, user["id"], len(steps))
    return lid, True


def requeue(conn, lid: int):
    """Run a failed launch again: what was removed or never made is made again; kept objects are checked and reused."""
    conn.execute("UPDATE launch_steps SET status='pending', error=NULL, external_id=CASE WHEN status='compensated' "
                 "THEN NULL ELSE external_id END WHERE launch_id=? AND status IN ('failed','compensated','running','pending')",
                 (lid,))
    conn.execute("UPDATE launches SET status='queued', error=NULL, next_at=NULL, lease_at=NULL, finished_at=NULL, "
                 "updated_at=?, approved_at=? WHERE id=? AND status IN ('failed','rolled_back')", (db.now(), _now_iso(), lid))
    conn.commit()


# ---------------------------------------------------------------- the saga

def _claim(conn, lid: int):
    cut = _now_iso(-LEASE_S)
    now = _now_iso()
    n = conn.execute("UPDATE launches SET status='launching', lease_at=?, runs=runs+1, updated_at=? WHERE id=? AND "
                     "status IN ('queued','launching') AND (lease_at IS NULL OR lease_at<?) AND (next_at IS NULL OR next_at<=?)",
                     (now, now, lid, cut, now)).rowcount
    conn.commit()
    return conn.execute("SELECT * FROM launches WHERE id=?", (lid,)).fetchone() if n else None


def _release(conn, lid: int, wait_s: float):
    conn.execute("UPDATE launches SET lease_at=NULL, next_at=?, updated_at=? WHERE id=?",
                 (_now_iso(max(1.0, wait_s)), db.now(), lid))
    conn.commit()


def _step_dict(s) -> dict:
    return {"no": s["step_no"], "key": s["step_key"], "kind": s["kind"], "name": s["name"],
            "request": json.loads(s["request_json"] or "{}"), "external_id": s["external_id"],
            "result": json.loads(s["result_json"] or "{}")}


def translate(err) -> str:
    """Plain words for a failure (the raw error is kept in launch_steps.error, scrubbed)."""
    if isinstance(err, adhttp.AdHttpError) and err.platform == "google":
        from adplatforms import google
        return google.translate(err)
    if isinstance(err, adhttp.AdHttpError) and err.platform in _video_platforms():
        import launch_video
        return launch_video.module(err.platform).translate(err)
    if isinstance(err, adhttp.NetworkError):
        return NETWORK_WORDS
    if isinstance(err, adhttp.AdHttpError):
        msg = ERRORS.get((err.code, err.subcode)) or ERRORS.get((err.code, None))
        if msg:
            return msg
        if isinstance(err, adhttp.TokenInvalid):
            return ERRORS[(190, None)]
        if isinstance(err, adhttp.PermissionDenied):
            return ERRORS[(200, None)]
        if err.user_message:
            return f"Meta said: {err.user_message[:300]}"
        return "Meta refused part of the campaign. The details are below."
    if isinstance(err, (OSError, KeyError)):
        return "A file or an earlier step was missing, so we stopped. Nothing was left running."
    return "Something went wrong on our side, so we stopped. Nothing was left running."


def _retryable(err) -> bool:
    if isinstance(err, adhttp.NetworkError):
        return True
    if isinstance(err, adhttp.AdHttpError) and not isinstance(err, (adhttp.TokenInvalid, adhttp.PermissionDenied)):
        if err.platform == "tiktok":
            return (err.status or 0) >= 500 or err.code in adhttp.TIKTOK_TRANSIENT
        return (err.status or 0) >= 500 or err.code in adhttp.META_TRANSIENT or err.code in adhttp.GOOGLE_TRANSIENT
    return False


def _record(conn, L, s, res: dict):
    ext = str(res.get("id") or "")
    conn.execute("UPDATE launch_steps SET status='done', external_id=?, result_json=?, error=NULL, finished_at=? WHERE id=?",
                 (ext, json.dumps(res), db.now(), s["id"]))
    kind = {"upload": "video" if res.get("object") == "video" else None, "check": None}.get(s["kind"], s["kind"])
    if kind and ext:
        parent = None
        if s["kind"] in ADSET_KINDS:
            parent = L["external_campaign_id"]
        elif s["kind"] == "ad":
            parent = (conn.execute("SELECT external_id FROM launch_steps WHERE launch_id=? AND step_key='adset'",
                                   (L["id"],)).fetchone() or {"external_id": None})["external_id"]
        conn.execute("INSERT INTO ad_objects (launch_id, workspace_id, platform, kind, external_id, parent_external_id, "
                     "name, status, created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(platform, external_id) DO UPDATE "
                     "SET status=excluded.status, launch_id=excluded.launch_id",
                     (L["id"], L["workspace_id"], L["platform"], kind, ext, parent, s["name"],
                      res.get("status") if res.get("status") in ("ENABLED", "PAUSED") else
                      {"video": "UPLOADED", "creative": "CREATED"}.get(kind, "PAUSED"), db.now()))
    if s["kind"] == "campaign":
        conn.execute("UPDATE launches SET external_campaign_id=? WHERE id=?", (ext, L["id"]))
    # Google makes the ad group and ad (or asset group) in the campaign's one mutate: one row each.
    parents = {"campaign": ext}
    for o in res.get("objects") or []:
        if not o.get("external_id"):
            continue
        conn.execute("INSERT INTO ad_objects (launch_id, workspace_id, platform, kind, external_id, parent_external_id, "
                     "name, status, created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(platform, external_id) DO UPDATE "
                     "SET status=excluded.status, launch_id=excluded.launch_id",
                     (L["id"], L["workspace_id"], L["platform"], o["kind"], o["external_id"], parents.get(o.get("parent")),
                      re.sub(r" \[cfy:\d+:[a-z0-9:]+\]$", "", o.get("name") or s["name"]), "ENABLED", db.now()))
        parents.setdefault(o["kind"], o["external_id"])
    conn.commit()


def run(lid: int) -> str | None:
    """Run (or continue) one launch. Returns its status after this pass ("waiting" when a step must wait), or
    None when it isn't due or another run holds it."""
    with db.connect() as conn:
        L = _claim(conn, lid)
        if L is None:
            return None
        conn_row = conn.execute("SELECT * FROM ad_connections WHERE id=? AND workspace_id=?",
                                (L["connection_id"], L["workspace_id"])).fetchone()
        try:
            if conn_row is None or conn_row["status"] != "active":
                raise adhttp.TokenInvalid(L["platform"], "the connection is not active")
            token = adplatforms.open_token(conn_row)
        except (adhttp.TokenInvalid, secretsbox.BoxError) as e:
            return _fail(conn, L, None, adhttp.TokenInvalid(L["platform"], str(e)), token=None, conn_row=conn_row)
        ad = adplatforms.adapter(L["platform"])
        base = {"launch_id": L["id"], "account": conn_row["external_account_id"], "token": token,
                "login": conn_row["business_id"] if L["platform"] == "google" else None}
        steps = conn.execute("SELECT * FROM launch_steps WHERE launch_id=? ORDER BY step_no", (L["id"],)).fetchall()
        results = {s["step_key"]: json.loads(s["result_json"] or "{}") for s in steps if s["status"] in ("done", "kept")}
        for s in steps:
            if s["status"] == "done":
                continue
            if s["status"] == "kept":     # kept by an earlier failed run: reuse it if it is still on Meta
                try:
                    alive = ad.verify(_step_dict(s), base)
                except adhttp.AdHttpError as e:
                    if isinstance(e, adhttp.RateLimited) or _retryable(e):
                        _release(conn, L["id"], max(5.0, float(getattr(e, "retry_after", 0) or RETRY_GAP_S)))
                        return "waiting"
                    return _fail(conn, L, s, e, token=token, conn_row=conn_row)
                if alive:
                    conn.execute("UPDATE launch_steps SET status='done' WHERE id=?", (s["id"],))
                    conn.commit()
                    continue
                conn.execute("UPDATE launch_steps SET status='pending', external_id=NULL, result_json=NULL WHERE id=?",
                             (s["id"],))
                conn.commit()
                results.pop(s["step_key"], None)
                s = conn.execute("SELECT * FROM launch_steps WHERE id=?", (s["id"],)).fetchone()
            conn.execute("UPDATE launch_steps SET status='running', attempts=attempts+1, started_at=COALESCE(started_at, ?) "
                         "WHERE id=?", (db.now(), s["id"]))
            conn.commit()
            ctx = dict(base, results=results, lookup=s["attempts"] > 0)
            try:
                res = ad.execute_step(_step_dict(s), ctx)
            except Exception as e:  # noqa: BLE001  sorted below; nothing here may leave the launch half-run
                waiting = _wait_for(conn, L, s, e)
                if waiting is not None:
                    return waiting
                return _fail(conn, L, s, e, token=token, conn_row=conn_row)
            _record(conn, L, s, res)
            L = conn.execute("SELECT * FROM launches WHERE id=?", (L["id"],)).fetchone()
            results[s["step_key"]] = res
        now = db.now()
        conn.execute("UPDATE launches SET status='paused', error=NULL, lease_at=NULL, next_at=NULL, finished_at=?, "
                     "updated_at=? WHERE id=?", (now, now, L["id"]))
        conn.execute("UPDATE approval_cards SET applied_at=?, error=NULL WHERE id=?", (now, L["card_id"]))
        conn.commit()
        log.info("launch %s done: campaign %s paused", L["id"], L["external_campaign_id"])
        return "paused"


def _wait_for(conn, L, s, e):
    """Put the step back to wait when the error says "later" (rate limit, video processing, a retryable error with
    tries left). Returns "waiting", or None when the launch should fail."""
    from adplatforms import meta
    waited = _age_s(L["approved_at"] or L["created_at"])
    if isinstance(e, (adhttp.RateLimited, meta.NotReady)):
        if waited > (meta.VIDEO_READY_WAIT_S if isinstance(e, meta.NotReady) else WAIT_MAX_S):
            return None
        gap = max(5.0, float(getattr(e, "retry_after", 15.0) or 15.0))
    elif _retryable(e) and s["attempts"] + 1 < STEP_ATTEMPTS:
        gap = RETRY_GAP_S * (s["attempts"] + 1)
    else:
        return None
    # A rate-limit or processing wait is not a failed try (nothing was made: our budget refused before the call,
    # Meta refused it, or the video check came first), so the attempt is given back.
    if isinstance(e, (adhttp.RateLimited, meta.NotReady)) and s["attempts"] > 0:
        conn.execute("UPDATE launch_steps SET attempts=MAX(0, attempts-1) WHERE id=?", (s["id"],))
    conn.execute("UPDATE launch_steps SET status='pending', error=? WHERE id=?",
                 (adhttp.scrub(str(e))[:500], s["id"]))
    conn.commit()
    _release(conn, L["id"], gap)
    log.info("launch %s step %s waits %d s: %s", L["id"], s["step_key"], int(gap), type(e).__name__)
    return "waiting"


def _fail(conn, L, s, err, token, conn_row) -> str:
    """Stop the launch: mark the step failed, undo what this launch made (compensate), report."""
    if isinstance(err, adhttp.AdHttpError):
        # str(err) is scrubbed (and "code=" reads as an OAuth code there), so the codes come from the attributes
        word = PLATFORM_WORDS.get(err.platform, err.platform)
        raw = (f"HTTP {err.status}, {word} code {err.code}" + (f", subcode {err.subcode}" if err.platform == "meta" else "")
               + ": " if err.code is not None else "") + str(err).split(": ", 1)[-1][:400] + \
            ((f" (fbtrace_id {err.trace})" if err.platform == "meta" else f" (request id {err.trace})") if err.trace else "")
    else:
        raw = f"{type(err).__name__}: " + adhttp.scrub(str(err))[:400]
    if s is not None:
        conn.execute("UPDATE launch_steps SET status='failed', error=?, finished_at=? WHERE id=?", (raw, db.now(), s["id"]))
        conn.commit()
    word = PLATFORM_WORDS.get(L["platform"], "the platform")
    if isinstance(err, adhttp.TokenInvalid) and conn_row is not None and conn_row["status"] == "active":
        adplatforms.mark(conn, conn_row["id"], "expired", f"{word} ended this access. Reconnect.")
    if isinstance(err, adhttp.RateLimited):
        from adplatforms import google
        message = google.WAITED_WORDS if L["platform"] == "google" else WAITED_WORDS
        if L["platform"] in _video_platforms():
            import launch_video
            message = launch_video.module(L["platform"]).WAITED_WORDS
    else:
        message = translate(err)
    complete = compensate(conn, L, token) if token else not _has_done(conn, L["id"])
    if not complete and not token:
        message += f" We couldn't reach {word} to remove what was made: the list below shows it, all PAUSED."
    status = "rolled_back" if complete else "failed"
    now = db.now()
    conn.execute("UPDATE launches SET status=?, error=?, lease_at=NULL, next_at=NULL, finished_at=?, updated_at=? WHERE id=?",
                 (status, message, now, now, L["id"]))
    conn.execute("UPDATE approval_cards SET error=? WHERE id=?", (message, L["card_id"]))
    conn.commit()
    log.warning("launch %s %s at step %s: %s", L["id"], status, s["step_key"] if s is not None else "-",
                type(err).__name__)
    return status


def _has_done(conn, lid: int) -> bool:
    return conn.execute("SELECT 1 FROM launch_steps WHERE launch_id=? AND status='done' AND external_id IS NOT NULL "
                        "AND kind IN ('creative','ad') LIMIT 1", (lid,)).fetchone() is not None


def compensate(conn, L, token) -> bool:
    """Undo, in reverse order, the steps this launch finished: delete ads and creatives, leave the campaign and ad set
    PAUSED (kept), keep uploads. True when nothing that could run or be reused by mistake is left behind."""
    ad = adplatforms.adapter(L["platform"])
    conn_row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (L["connection_id"],)).fetchone()
    ctx = {"launch_id": L["id"], "account": conn_row["external_account_id"], "token": token, "max_wait": COMP_WAIT_S,
           "login": conn_row["business_id"] if L["platform"] == "google" else None}
    ok = True
    for s in conn.execute("SELECT * FROM launch_steps WHERE launch_id=? AND status='done' ORDER BY step_no DESC",
                          (L["id"],)).fetchall():
        try:
            outcome = ad.compensate(_step_dict(s), ctx)
        except adhttp.AdHttpError as e:
            ok = False
            conn.execute("UPDATE launch_steps SET error=? WHERE id=?",
                         ((f"Couldn't remove it on {PLATFORM_WORDS.get(L['platform'], 'the platform')}: " + str(e))[:500],
                          s["id"]))
            conn.commit()
            continue
        conn.execute("UPDATE launch_steps SET status=? WHERE id=?", (outcome, s["id"]))
        if outcome == "compensated" and s["external_id"]:
            conn.execute("UPDATE ad_objects SET status='DELETED' WHERE platform=? AND external_id=?",
                         (L["platform"], s["external_id"]))
        conn.commit()
    return ok


def due(conn, limit: int = 3) -> list:
    now = _now_iso()
    cut = _now_iso(-LEASE_S)
    return [r["id"] for r in conn.execute(
        "SELECT id FROM launches WHERE status IN ('queued','launching') AND (next_at IS NULL OR next_at<=?) AND "
        "(lease_at IS NULL OR lease_at<?) ORDER BY id LIMIT ?", (now, cut, limit)).fetchall()]


def tick(limit: int = 3) -> int:
    """The worker's pass: run due launches whose workspace still has AD_LAUNCH. Returns how many ran."""
    n = 0
    with db.connect() as conn:
        ids = due(conn, limit)
        wss = {}
        for lid in ids:
            row = conn.execute("SELECT workspace_id, platform FROM launches WHERE id=?", (lid,)).fetchone()
            wss[lid] = (conn.execute("SELECT * FROM workspaces WHERE id=?", (row["workspace_id"],)).fetchone(),
                        row["platform"])
    for lid in ids:
        w, platform = wss.get(lid) or (None, "meta")
        if not adplatforms.platform_on(platform, w):
            continue                # the flag is off for it (AD_LAUNCH; GOOGLE_ADS for Google): it waits, PAUSED
        try:
            run(lid)
            n += 1
        except Exception:  # noqa: BLE001  one bad launch never stops the worker; its lease runs out and it retries
            log.exception("launch %s run failed", lid)
    return n


def maybe_tick():
    """The worker loop's call: at most every TICK_SECONDS, never raises."""
    if time.time() - _last_tick[0] < TICK_SECONDS:
        return
    _last_tick[0] = time.time()
    try:
        tick()
    except Exception:  # noqa: BLE001
        log.exception("launch tick failed")


# ---------------------------------------------------------------- what the pages show

STEP_WORDS = {"upload": "Upload {what}", "creative": "Create the ad creative for {what}", "campaign": "Create the campaign",
              "adset": "Create the ad set", "ad": "Create {what}", "check": "Check it on Google",
              "adsquad": "Create the ad squad", "adgroup": "Create the ad group"}


def step_label(s) -> str:
    key, kind = s["step_key"], s["kind"]
    n = key.split(":")[1] if ":" in key else ""
    if kind == "upload":
        req = json.loads(s["request_json"] or "{}")
        if req.get("field"):                       # Google Performance Max: one image asset per step
            from adplatforms import google
            return f"Upload the {google.FIELD_WORDS.get(req['field'], 'image')}" + \
                (f" from ad {_ad_no(req)}" if req.get("asset_id") else "")
        return f"Upload ad {n}'s {req.get('media') or 'file'}"
    if kind == "campaign" and s["request_json"] and '"operations"' in (s["request_json"] or ""):
        typ = json.loads(s["request_json"]).get("type")
        return "Create the campaign, " + ("its asset group and assets in one go" if typ == "pmax"
                                          else "ad group, keywords and ad in one go")
    if kind == "creative":
        return f"Create ad {n}'s creative"
    if kind == "ad":
        return f"Create ad {n}"
    return STEP_WORDS[kind]


def _ad_no(req: dict):
    return req.get("ad_no") or req.get("asset_id")


def ads_manager_url(account_id: str, campaign_id: str | None = None) -> str:
    u = f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={account_id}"
    return u + (f"&selected_campaign_ids={campaign_id}" if campaign_id else "")


def view(conn, L) -> dict:
    steps = conn.execute("SELECT * FROM launch_steps WHERE launch_id=? ORDER BY step_no", (L["id"],)).fetchall()
    objs = conn.execute("SELECT * FROM ad_objects WHERE launch_id=? ORDER BY id", (L["id"],)).fetchall()
    card = conn.execute("SELECT * FROM approval_cards WHERE id=?", (L["card_id"] or 0,)).fetchone()
    conn_row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (L["connection_id"],)).fetchone()
    account = conn_row["external_account_id"] if conn_row else ""
    labels = {"pending": "Waiting", "running": "Working", "done": "Done", "failed": "Failed",
              "compensated": "Removed", "kept": "Kept"}
    return {"launch": L, "card": json.loads(card["after_json"]) if card and card["after_json"] else None,
            "steps": [{"label": step_label(s), "status": s["status"], "word": labels.get(s["status"], s["status"]),
                       "error": s["error"] if s["status"] in ("failed",) or (s["error"] and s["status"] == "done") else None,
                       "external_id": s["external_id"], "kind": s["kind"]} for s in steps],
            "objects": [dict(o) for o in objs], "account": account, "platform": L["platform"],
            "word": PLATFORM_WORDS.get(L["platform"], "Meta"),
            "manager": manager_url(L["platform"], account, L["external_campaign_id"]),
            "review": next((json.loads(s["result_json"] or "{}") for s in steps if s["kind"] == "check"
                            and s["status"] == "done"), None),
            "busy": L["status"] in ACTIVE,
            "left": [dict(o) for o in objs if o["status"] != "DELETED" and o["kind"] in ("creative", "ad")]
            if L["status"] == "failed" else []}


def manager_url(platform: str, account: str, campaign_id: str | None = None) -> str | None:
    """The platform's own ads manager for this account (and campaign, on Meta), or None without an account."""
    if not account:
        return None
    if platform == "google":
        return _google_url(account)
    if platform in _video_platforms():
        return _video_url(platform, account)
    return ads_manager_url(account, campaign_id)


MANAGER_WORDS = {"meta": "Ads Manager", "google": "Google Ads", "tiktok": "TikTok Ads Manager",
                 "snapchat": "Snapchat Ads Manager"}


def _google_url(account: str) -> str:
    from adplatforms import google
    return google.ads_url(account)


def _video_url(platform: str, account: str) -> str:
    import launch_video
    return launch_video.module(platform).ads_url(account)


def latest_for_plan(conn, ws_id: int, cid: int, platform: str = None):
    if platform:
        return conn.execute("SELECT * FROM launches WHERE workspace_id=? AND plan_id=? AND platform=? ORDER BY id DESC "
                            "LIMIT 1", (int(ws_id), int(cid), platform)).fetchone()
    return conn.execute("SELECT * FROM launches WHERE workspace_id=? AND plan_id=? ORDER BY id DESC LIMIT 1",
                        (int(ws_id), int(cid))).fetchone()

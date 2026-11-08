"""The internal campaign spec (roadmap M2.2, design doc section 3): one platform-neutral description
of a campaign that each ad-platform adapter maps to its own objects (M2.3 on). Users think in
objective, audience, budget, creative and schedule, not in platform object graphs.

    spec, issues = adspec.from_dict(data)     # shape check against SCHEMA, then the dataclasses
    issues += adspec.validate(spec)           # platform constraints (budgets, text lengths, ratios, ...)
    adspec.to_dict(spec) / adspec.spec_hash(spec)

adpolicy.preflight(spec) runs both plus the policy linter; the launch button stays off while any
Issue has level "block". Nothing here talks to a platform.

Money is in minor units of the ad account's currency (cents for USD), as the platforms take it.
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

PLATFORMS = ("meta", "google", "tiktok", "snapchat")
OBJECTIVES = ("sales", "leads", "traffic", "awareness")
VERTICALS = ("general", "health", "supplements", "weight_loss", "cosmetics", "finance", "crypto", "alcohol",
             "housing", "credit", "employment")
SPECIAL_AD_CATEGORIES = ("housing", "credit", "employment", "social_issues")
ASPECTS = ("9:16", "4:5", "1:1", "16:9", "1.91:1")
KINDS = ("image", "video")
CTAS = ("SHOP_NOW", "LEARN_MORE", "SIGN_UP", "BOOK_NOW", "CONTACT_US", "GET_QUOTE", "ORDER_NOW", "SUBSCRIBE",
        "APPLY_NOW", "DOWNLOAD", "GET_OFFER", "CALL_NOW")
EVENTS = ("Purchase", "Lead", "CompleteRegistration", "AddToCart", "InitiateCheckout", "Contact", "Schedule",
          "SubmitApplication", "ViewContent")
PLACEMENTS = ("feed", "stories", "reels", "right_column", "search", "explore", "audience_network", "messenger",
              "in_stream", "tiktok_feed", "snap_stories", "youtube", "discover")

# Platform constraints (sources: research B1 to B4 and the platforms' ad specs, as of 2026-11-07).
# hard: the platform refuses it. soft: it runs, but gets cut off or delivers worse.
TEXT_LIMITS = {
    #            field: (soft, hard)
    "meta": {"primary_text": (125, 2200), "headline": (40, 255), "description": (30, 255)},
    "tiktok": {"primary_text": (100, 100), "headline": (40, 100), "description": (None, None)},
    "snapchat": {"primary_text": (None, None), "headline": (34, 34), "description": (None, None)},
    "google": {"primary_text": (90, 90), "headline": (30, 30), "description": (90, 90)},
}
# Smallest daily budget we let through, in minor units of USD-like currencies (platform minimums
# vary by currency and objective; these are the common floors).
MIN_DAILY = {"meta": 100, "google": 100, "tiktok": 2000, "snapchat": 500}
# Aspect ratios each platform takes for in-feed video/image (first is the preferred one).
ASPECTS_OK = {"meta": ("4:5", "1:1", "9:16", "1.91:1", "16:9"), "tiktok": ("9:16", "1:1", "16:9"),
              "snapchat": ("9:16",), "google": ("1.91:1", "1:1", "4:5", "9:16", "16:9")}
OBJECTIVES_BY_PLATFORM = {"meta": OBJECTIVES, "google": OBJECTIVES, "tiktok": OBJECTIVES, "snapchat": OBJECTIVES}
MAX_CREATIVES = 50                     # Meta: ads per ad set
RSA_HEADLINES, RSA_DESCRIPTIONS = (3, 15), (2, 4)   # Google responsive search ads (research B2)


@dataclass
class Issue:
    """One finding from the validator or the policy linter. level "block" keeps the launch button off."""
    level: str                 # block | warn
    rule_id: str               # stable id, e.g. "meta.text.headline_long", "policy.ai_testimonial"
    message: str               # plain words, shown to the member
    fix: str = ""              # what to change
    where: str = ""            # the field, e.g. "creatives[0].headline"

    def as_dict(self):
        return asdict(self)


@dataclass
class CreativeSpec:
    kind: str = "image"                    # image | video
    asset_id: Optional[int] = None         # outputs.id of the ad in the library (provenance: adpolicy.provenance)
    name: str = ""
    primary_text: str = ""
    headline: str = ""
    description: str = ""
    script: str = ""                       # spoken words of a video
    on_screen_text: str = ""
    cta: str = "LEARN_MORE"
    destination_url: str = ""
    utm: str = ""                          # e.g. "utm_source=meta&utm_medium=paid&utm_campaign={campaign}"
    aspect: str = "4:5"
    ai_presenter: bool = False             # an AI presenter is on screen
    synthetic_voice: bool = False          # an AI voice speaks


@dataclass
class CampaignSpec:
    objective: str
    platform: str
    name: str
    daily_budget_minor: int
    currency: str
    start_at: Optional[str] = None         # ISO 8601
    end_at: Optional[str] = None
    geo: dict = field(default_factory=dict)        # {"countries": ["US"], "regions": [...], "cities": [{"name", "radius_km"}], "zips": [...]}
    audience: dict = field(default_factory=dict)   # {"age_min": 18, "age_max": 65, "genders": ["all"], "interests": [...]}
    placements: object = "auto"                    # "auto" | [placement, ...]
    conversion: Optional[dict] = None              # {"pixel_id", "event", "landing_url"}
    creatives: list = field(default_factory=list)  # [CreativeSpec]
    special_ad_categories: list = field(default_factory=list)
    ai_disclosure: bool = False
    google: Optional[dict] = None                  # {"type": "search"|"pmax", "keywords", "negative_keywords", "headlines", "descriptions"}
    vertical: str = "general"                      # what is advertised (policy rules per vertical, research B6)
    claims_substantiated: bool = False             # the member confirmed they can back up any result claim


# JSON schema of a CampaignSpec (draft 2020-12), for the launch page, the MCP tools and stored launches.
_STR = {"type": "string"}
CREATIVE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind"],
    "properties": {
        "kind": {"enum": list(KINDS)}, "asset_id": {"type": ["integer", "null"]}, "name": {"type": "string", "maxLength": 200},
        "primary_text": {"type": "string", "maxLength": 5000}, "headline": {"type": "string", "maxLength": 500},
        "description": {"type": "string", "maxLength": 500}, "script": {"type": "string", "maxLength": 5000},
        "on_screen_text": {"type": "string", "maxLength": 1000}, "cta": {"enum": list(CTAS)},
        "destination_url": {"type": "string", "maxLength": 2000}, "utm": {"type": "string", "maxLength": 500},
        "aspect": {"enum": list(ASPECTS)}, "ai_presenter": {"type": "boolean"}, "synthetic_voice": {"type": "boolean"}}}
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "title": "CampaignSpec", "type": "object",
    "additionalProperties": False,
    "required": ["objective", "platform", "name", "daily_budget_minor", "currency", "creatives"],
    "properties": {
        "objective": {"enum": list(OBJECTIVES)}, "platform": {"enum": list(PLATFORMS)},
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "daily_budget_minor": {"type": "integer", "minimum": 1},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "start_at": {"type": ["string", "null"]}, "end_at": {"type": ["string", "null"]},
        "geo": {"type": "object"}, "audience": {"type": "object"},
        "placements": {"oneOf": [{"const": "auto"}, {"type": "array", "items": {"enum": list(PLACEMENTS)}}]},
        "conversion": {"type": ["object", "null"]},
        "creatives": {"type": "array", "items": CREATIVE_SCHEMA},
        "special_ad_categories": {"type": "array", "items": {"enum": list(SPECIAL_AD_CATEGORIES)}},
        "ai_disclosure": {"type": "boolean"}, "google": {"type": ["object", "null"]},
        "vertical": {"enum": list(VERTICALS)}, "claims_substantiated": {"type": "boolean"}}}


# ---------------------------------------------------------------- shape (a small checker for SCHEMA; no dependency)

_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _type_ok(value, t):
    ts = t if isinstance(t, list) else [t]
    for name in ts:
        py = _TYPES[name]
        if name == "integer" and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def _check(value, schema, where, out):
    if "oneOf" in schema:
        if not any(not _check(value, s, where, []) for s in schema["oneOf"]):
            out.append(Issue("block", "spec.shape", f"{where or 'value'} is not allowed here.", "", where))
        return out
    if "const" in schema and value != schema["const"]:
        out.append(Issue("block", "spec.shape", f"{where} must be {schema['const']!r}.", "", where))
        return out
    if "enum" in schema and value not in schema["enum"]:
        out.append(Issue("block", "spec.shape", f"{where} must be one of: {', '.join(map(str, schema['enum']))}.", "", where))
        return out
    if "type" in schema and not _type_ok(value, schema["type"]):
        out.append(Issue("block", "spec.shape", f"{where or 'The spec'} has the wrong type.", "", where))
        return out
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10 ** 9):
            out.append(Issue("block", "spec.shape", f"{where} is too short or too long.", "", where))
        if "pattern" in schema and not re.match(schema["pattern"], value):
            out.append(Issue("block", "spec.shape", f"{where} is not in the right format.", "", where))
    if isinstance(value, int) and not isinstance(value, bool) and value < schema.get("minimum", -10 ** 18):
        out.append(Issue("block", "spec.shape", f"{where} must be at least {schema['minimum']}.", "", where))
    if isinstance(value, dict) and "properties" in schema:
        for k in schema.get("required", []):
            if k not in value:
                out.append(Issue("block", "spec.missing", f"{k} is missing.", "", f"{where}.{k}".lstrip(".")))
        for k, v in value.items():
            sub = schema["properties"].get(k)
            if sub is None:
                if schema.get("additionalProperties") is False:
                    out.append(Issue("block", "spec.unknown_field", f"{k} is not a spec field.", "", f"{where}.{k}".lstrip(".")))
                continue
            _check(v, sub, f"{where}.{k}".lstrip("."), out)
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            _check(v, schema["items"], f"{where}[{i}]", out)
    return out


def shape_issues(data) -> list:
    return _check(data, SCHEMA, "", [])


def from_dict(data) -> tuple:
    """(CampaignSpec or None, shape issues). None when the shape is wrong."""
    issues = shape_issues(data)
    if issues:
        return None, issues
    d = dict(data)
    d["creatives"] = [CreativeSpec(**c) for c in d.get("creatives") or []]
    return CampaignSpec(**d), []


def to_dict(spec: CampaignSpec) -> dict:
    return asdict(spec)


def spec_hash(spec: CampaignSpec) -> str:
    """sha256 of the canonical JSON: part of a launch's idempotency key (M2.3)."""
    return hashlib.sha256(json.dumps(to_dict(spec), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------- platform constraints

def _iso(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return False


def _url_ok(u: str) -> bool:
    return bool(re.match(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/[^\s]*)?$", u or ""))


def validate(spec: CampaignSpec, now: datetime = None) -> list:
    """Platform constraints: budget floors, dates, targeting, conversion setup, creative counts,
    text lengths, aspect ratios, Google RSA counts. The policy rules are in adpolicy.py."""
    now = now or datetime.now(timezone.utc)
    p, out = spec.platform, []
    add = lambda *a: out.append(Issue(*a))  # noqa: E731
    if spec.objective not in OBJECTIVES_BY_PLATFORM.get(p, ()):
        add("block", f"{p}.objective", f"{spec.objective} isn't available on {p}.", "Pick another objective.", "objective")
    if spec.daily_budget_minor < MIN_DAILY.get(p, 100):
        add("block", f"{p}.budget_min", f"The daily budget is under {p}'s minimum.",
            f"Set at least {MIN_DAILY.get(p, 100) / 100:.2f} {spec.currency} a day.", "daily_budget_minor")
    start, end = _iso(spec.start_at), _iso(spec.end_at)
    if start is False or end is False:
        add("block", "spec.dates", "A start or end date isn't a valid date.", "Use the date picker.", "start_at")
    else:
        if end and end <= now:
            add("block", "spec.dates", "The end date is in the past.", "Pick a later end date.", "end_at")
        if start and end and end <= start:
            add("block", "spec.dates", "The end date is before the start date.", "Swap or fix the dates.", "end_at")
    geo = spec.geo or {}
    countries = geo.get("countries") or []
    if not countries and not geo.get("regions") and not geo.get("cities") and not geo.get("zips"):
        add("block", "spec.geo_missing", "No location is set.", "Add at least one country.", "geo")
    if any(not re.match(r"^[A-Z]{2}$", str(c)) for c in countries):
        add("block", "spec.geo_country", "A country isn't a 2-letter code (like US).", "Use codes such as US, CA, GB.", "geo.countries")
    aud = spec.audience or {}
    lo, hi = aud.get("age_min", 18), aud.get("age_max", 65)
    if not (isinstance(lo, int) and isinstance(hi, int)) or lo < 13 or hi > 65 or lo > hi:
        add("block", "spec.age_range", "The age range must be between 13 and 65, lowest first.", "Fix the ages.", "audience")
    if spec.objective in ("sales", "leads"):
        conv = spec.conversion or {}
        if not conv.get("pixel_id") and p in ("meta", "tiktok", "snapchat"):
            add("block", f"{p}.conversion_pixel", f"A {spec.objective} campaign needs a pixel to optimize for.",
                "Pick the pixel in Settings > Connections, or choose Traffic.", "conversion.pixel_id")
        if conv and conv.get("event") and conv["event"] not in EVENTS:
            add("block", "spec.conversion_event", "That conversion event isn't one we support.", "Pick Purchase or Lead.",
                "conversion.event")
    if p == "meta" and not isinstance(spec.special_ad_categories, list):
        add("block", "meta.special_ad_categories", "Meta needs the special ad categories field.", "", "special_ad_categories")
    if spec.placements != "auto" and p == "snapchat" and spec.placements and set(spec.placements) - {"snap_stories"}:
        add("block", "snapchat.placements", "Snapchat ads run in Stories only here.", "Use automatic placements.", "placements")

    # creatives
    if p != "google" or (spec.google or {}).get("type") != "search":
        if not spec.creatives:
            add("block", "spec.no_creatives", "The campaign has no ads.", "Add at least one ad.", "creatives")
        if len(spec.creatives) > MAX_CREATIVES:
            add("block", "spec.too_many_creatives", f"Up to {MAX_CREATIVES} ads per campaign.", "Remove some ads.", "creatives")
    limits = TEXT_LIMITS.get(p, {})
    for i, c in enumerate(spec.creatives):
        w = f"creatives[{i}]"
        for fld in ("primary_text", "headline", "description"):
            soft, hard = limits.get(fld, (None, None))
            n = len(getattr(c, fld) or "")
            if hard and n > hard:
                add("block", f"{p}.text.{fld}_too_long", f"The {fld.replace('_', ' ')} is {n} characters; {p} allows {hard}.",
                    f"Cut it to {hard} characters.", f"{w}.{fld}")
            elif soft and n > soft:
                add("warn", f"{p}.text.{fld}_long", f"The {fld.replace('_', ' ')} is {n} characters; {p} cuts it off after "
                    f"about {soft}.", f"Keep it under {soft} characters.", f"{w}.{fld}")
        if p == "tiktok" and not (c.primary_text or "").strip():
            add("block", "tiktok.text.primary_text_missing", "TikTok needs ad text (1 to 100 characters).", "Add ad text.",
                f"{w}.primary_text")
        if not _url_ok(c.destination_url):
            add("block", "spec.destination_url", "The landing page must be a full https:// address.",
                "Paste the page address, starting with https://", f"{w}.destination_url")
        if c.aspect not in ASPECTS_OK.get(p, ASPECTS):
            add("block", f"{p}.aspect", f"{p} doesn't take {c.aspect} ads.", f"Export it at {ASPECTS_OK[p][0]}.", f"{w}.aspect")
        elif p == "tiktok" and c.aspect != "9:16":
            add("warn", "tiktok.aspect_not_vertical", "TikTok ads do best full-screen at 9:16.", "Export it at 9:16.", f"{w}.aspect")
        if p == "meta" and isinstance(spec.placements, list) and {"stories", "reels"} & set(spec.placements) \
                and c.aspect not in ("9:16",):
            add("warn", "meta.aspect_stories", "Stories and Reels show 9:16; this ad gets bars or a crop.",
                "Add a 9:16 version.", f"{w}.aspect")
        if p in ("tiktok", "snapchat") and c.kind != "video":
            add("block", f"{p}.video_only", f"{p} in-feed ads here are video.", "Pick a video ad.", f"{w}.kind")
    if p == "meta" and spec.placements == "auto" and spec.creatives and \
            not any(c.aspect == "9:16" for c in spec.creatives):
        add("warn", "meta.aspect_auto_no_vertical", "Automatic placements include Stories and Reels, and no ad is 9:16.",
            "Add a 9:16 version of at least one ad.", "creatives")

    # Google responsive search ads
    if p == "google" and (spec.google or {}).get("type", "search") == "search":
        g = spec.google or {}
        hs, ds = g.get("headlines") or [], g.get("descriptions") or []
        if not RSA_HEADLINES[0] <= len(hs) <= RSA_HEADLINES[1]:
            add("block", "google.rsa_headlines", f"Search ads need 3 to 15 headlines (this has {len(hs)}).", "", "google.headlines")
        if not RSA_DESCRIPTIONS[0] <= len(ds) <= RSA_DESCRIPTIONS[1]:
            add("block", "google.rsa_descriptions", f"Search ads need 2 to 4 descriptions (this has {len(ds)}).", "",
                "google.descriptions")
        for i, h in enumerate(hs):
            if len(h) > 30:
                add("block", "google.rsa_headline_long", f"Headline {i + 1} is {len(h)} characters; Google allows 30.", "",
                    f"google.headlines[{i}]")
        for i, d in enumerate(ds):
            if len(d) > 90:
                add("block", "google.rsa_description_long", f"Description {i + 1} is {len(d)} characters; Google allows 90.",
                    "", f"google.descriptions[{i}]")
        if not g.get("keywords"):
            add("block", "google.keywords", "A search campaign needs keywords.", "Add a few keywords.", "google.keywords")
    return out

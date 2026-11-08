"""The service-business entrance (roadmap M5.2; UX-13, UX-14, UX-15, UX-16), behind RS_SERVICE_PATH (off by default).

A plumber, dentist or agency has a website, not a product page or a product photo. With the flag on:

  * the first run asks "What do you sell?" (a product or a service); the service choice takes the website URL
    (read_site: business name, logo, colors, up to 3 photos from the site, up to 5 suggested claims in the site's own
    words, phone, service area and headline, all without AI so nothing is charged), skips the photo requirement, and
    makes the 2 free image ads from a site photo or from a template that needs no photo (TEMPLATES);
  * the product first run also downloads up to 3 of the page's own photos and suggests up to 5 claims from the
    page's words (page_claims), where it found 1 photo and 0 claims before;
  * the planner gets a service-area field when the goal is leads, a Google keyword list whenever Google is picked
    (the model's own list, else starter_keywords from the services and area), and a leads plan always lists
    "Publish the lead page", "Add the quote form to it" and "Turn on the follow-up flow" with links (lead_steps).

Claims found here are suggestions only: they land in products.claims_pending and the member approves them, exactly
like the AI importer's. A product is a service when its import_json says {"sells": "service"} (is_service).
"""
import json
import os
import re
from urllib.parse import urljoin, urlsplit

import compliance
import fetcher
import product_import
import seo


def on() -> bool:
    """RS_SERVICE_PATH=1 turns the service entrance on (off by default)."""
    return os.environ.get("RS_SERVICE_PATH", "0").strip().lower() in ("1", "on", "true", "yes")


def is_service(product) -> bool:
    """The product was set up as a service (first run "A service", or a website import)."""
    if product is None:
        return False
    keys = product.keys() if hasattr(product, "keys") else []
    if "import_json" not in keys:
        return False
    try:
        return (json.loads(product["import_json"] or "{}") or {}).get("sells") == "service"
    except (ValueError, TypeError, AttributeError):
        return False


def site_info(product) -> dict:
    """What the website import found (logo, colors, area, headline, phone), or {}."""
    if product is None:
        return {}
    try:
        return (json.loads(product["import_json"] or "{}") or {}).get("site") or {}
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------- reading a business website

BUSINESS_TYPES = re.compile(r"(Business|Service|Organization|Organisation|Store|Office|Clinic|Dentist|Physician|"
                            r"Attorney|LegalService|Contractor|Plumber|Electrician|Locksmith|RoofingContractor|"
                            r"HVACBusiness|MovingCompany|AutoRepair|Restaurant|Salon|SpaOrSalon|HealthClub|Agency|"
                            r"RealEstateAgent|AccountingService|FinancialService|Hotel|Physiotherapy|Optician|"
                            r"VeterinaryCare|ChildCare|School|Gym|Corporation|ProfessionalService)$")
SEP = re.compile(r"\s+[|–—\-:·•]\s+")
HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
CTA_START = re.compile(r"^(get|call|book|contact|learn|shop|buy|see|click|read|sign|schedule|request|view|order|"
                       r"start|find|join|subscribe|download|watch|explore|discover|apply|text|email|chat|follow)\b", re.I)
NOT_CLAIM = re.compile(r"(\bcookies?\b|privacy|terms|copyright|©|all rights|\bmenu\b|\bcart\b|log ?in|sign ?in|"
                       r"\bsearch\b|skip to|javascript|subscribe|newsletter|\bhome\b|\babout us\b|\bfaq\b|\bblog\b|"
                       r"\bbest\b|#1|\bnumber one\b|guarantee|cheapest|top[- ]rated|\bleading\b|\bperfect\b|"
                       r"\bamazing\b|\bincredible\b|\bworld[- ]class\b|\bunbeatable\b|\breviews?\b|\bstars?\b|\?)", re.I)


def _business_ld(blocks) -> dict:
    """The page's schema.org LocalBusiness / Organization (or a subtype), or {}."""
    found = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            ts = [str(x) for x in (t if isinstance(t, list) else [t]) if x]
            if any(BUSINESS_TYPES.search(x) for x in ts) and o.get("name"):
                found.append(o)
            for k in ("@graph", "mainEntity", "publisher", "provider", "author"):
                if k in o:
                    walk(o[k])
    for b in blocks:
        walk(b)
    # A LocalBusiness subtype beats a bare Organization (often the web agency's or the parent's).
    found.sort(key=lambda o: 0 if any(k in o for k in ("address", "telephone", "areaServed")) else 1)
    return found[0] if found else {}


def _words(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def business_name(ld: dict, meta: dict, title: str, url: str) -> str:
    """The business's own name: schema.org first, then og:site_name, then the part of the <title> that matches the
    domain (titles read "Name | what we do", or the other way round). Never the whole title with its slogan."""
    if ld.get("name"):
        return " ".join(str(ld["name"]).split())[:80]
    if meta.get("og:site_name"):
        return " ".join(meta["og:site_name"].split())[:80]
    parts = [p.strip() for p in SEP.split(" ".join((title or "").split())) if p.strip()]
    if not parts:
        return product_import.brand_guess(url)
    dom = re.sub(r"[^a-z0-9]", "", product_import.brand_guess(url).lower())
    if dom:
        for p in parts:
            squashed = re.sub(r"[^a-z0-9]", "", p.lower())
            if squashed and (squashed in dom or dom in squashed or
                             any(len(w) > 3 and w in dom for w in _words(p))):
                return p[:80]
    return parts[0][:80]


def _logo_url(ld, p, url, links) -> str:
    logo = ld.get("logo")
    if isinstance(logo, dict):
        logo = logo.get("url") or logo.get("contentUrl")
    cands = [logo] if isinstance(logo, str) else []
    cands += [i.get("src") for i in p.images if re.search(r"logo", (i.get("src") or "") + " " + (i.get("alt") or ""), re.I)]
    cands += links.get("apple-touch-icon", [])
    for c in cands:
        c = (c or "").strip()
        if c and not c.startswith("data:") and not c.lower().split("?")[0].endswith((".svg", ".ico", ".gif")):
            absu = urljoin(url, c)
            if urlsplit(absu).scheme in ("http", "https"):
                return absu
    return ""


def _link_rels(html: str) -> dict:
    out = {}
    for m in re.finditer(r"<link\b[^>]*>", html or "", re.I):
        tag = m.group(0)
        rel = re.search(r'rel=["\']([^"\']+)', tag, re.I)
        href = re.search(r'href=["\']([^"\']+)', tag, re.I)
        if rel and href:
            for r in rel.group(1).lower().split():
                out.setdefault(r, []).append(href.group(1))
    return out


def _norm_hex(h: str) -> str:
    h = h.lstrip("#").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def _vivid(h: str) -> bool:
    """Not white, black or a grey: a colour worth calling the brand's."""
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    hi, lo = max(r, g, b), min(r, g, b)
    return hi - lo >= 40 and 30 <= hi and lo <= 235


def css_colors(html: str, meta: dict) -> list:
    """Brand colours the page states: theme-color first, then the most used vivid hex colours in its own CSS."""
    out = []
    tc = (meta.get("theme-color") or meta.get("msapplication-tilecolor") or "").strip()
    if HEX.fullmatch(tc or "x"):
        out.append(_norm_hex(tc))
    styles = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", html or "", re.I | re.S))
    styles += " " + " ".join(re.findall(r'style=["\']([^"\']+)', html or "", re.I))
    counts = {}
    for m in HEX.finditer(styles):
        h = _norm_hex(m.group(0))
        counts[h] = counts.get(h, 0) + 1
    for h, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
        if h not in out:
            out.append(h)
    return [h for h in out if _vivid(h)][:3]


def image_colors(data: bytes) -> list:
    """The 2 or 3 main vivid colours of a logo image (PIL), as hex."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((96, 96))
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im).convert("RGB").quantize(colors=6)
        pal = im.getpalette() or []
        counts = sorted(im.getcolors() or [], reverse=True)
    except Exception:  # noqa: BLE001  a logo we can't read just gives no colours
        return []
    out = []
    for n, idx in counts:
        h = "#%02x%02x%02x" % tuple(pal[idx * 3: idx * 3 + 3])
        if _vivid(h) and h not in out:
            out.append(h)
    return out[:3]


# A few plain colour names, so a brief can say "deep blue and amber" instead of a hex code (which an image model
# may print on the ad).
_NAMES = [("black", (20, 20, 20)), ("white", (245, 245, 245)), ("grey", (128, 128, 128)), ("red", (200, 40, 40)),
          ("dark red", (120, 20, 30)), ("orange", (240, 130, 30)), ("amber", (240, 170, 20)), ("yellow", (245, 215, 50)),
          ("green", (50, 160, 70)), ("dark green", (20, 90, 50)), ("teal", (20, 140, 140)), ("light blue", (100, 170, 230)),
          ("blue", (40, 100, 210)), ("deep blue", (25, 60, 120)), ("navy", (20, 30, 70)), ("purple", (120, 60, 170)),
          ("pink", (230, 110, 160)), ("brown", (120, 80, 40)), ("beige", (220, 200, 170))]


def color_name(h: str) -> str:
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return min(_NAMES, key=lambda nc: sum((a - c) ** 2 for a, c in zip((r, g, b), nc[1])))[0]


def _areas(ld) -> list:
    v = ld.get("areaServed")
    vals = v if isinstance(v, list) else [v] if v else []
    out = []
    for x in vals:
        name = x.get("name") if isinstance(x, dict) else x
        if isinstance(name, str) and name.strip() and name.strip() not in out:
            out.append(" ".join(name.split())[:60])
    if not out and isinstance(ld.get("address"), dict):
        city = ld["address"].get("addressLocality")
        if isinstance(city, str) and city.strip():
            out.append(city.strip()[:60])
    return out[:6]


def _sentences(text: str) -> list:
    return [s.strip(" -–—•·,;") for s in re.split(r"(?<=[.!])\s+|\s*[\n•·|]\s*", text or "") if s.strip(" -–—•·,;")]


def page_claims(facts: dict, name: str = "", limit: int = 5) -> list:
    """Up to `limit` short facts in the page's own words (its description, paragraphs and headings), for the member
    to approve. No AI: sentences of 2 to 14 words that pass the compliance rules, without superlatives, calls to action,
    questions or site furniture, and none that only repeats a longer one."""
    # The page text runs the title and headings into the next line ("Hearth & Co Ember Candle Amber and cedar"):
    # cut it at each heading and at the name and brand, so only the sentences around them are left.
    text = facts.get("text") or ""
    names = [x for x in [name, facts.get("name"), facts.get("brand")] + list(facts.get("headings") or []) if x]
    for x in sorted(set(names), key=len, reverse=True):
        text = text.replace(x, " | ")
    sources = [facts.get("description") or ""] + list(facts.get("paragraphs") or []) + \
        list(facts.get("headings") or []) + [text]
    seen, seen_sets, out = set(), [], []
    skip = {" ".join(re.findall(r"[a-z0-9]+", str(x).lower())) for x in (name, facts.get("name"), facts.get("brand")) if x}
    for src in sources:
        for s in _sentences(src):
            s = s.rstrip(".!").strip()
            words = s.split()
            if not (2 <= len(words) <= 14) or not (6 <= len(s) <= 120) or not re.search(r"[A-Za-z]{3}", s):
                continue
            key = " ".join(re.findall(r"[a-z0-9]+", s.lower()))
            if key in skip:
                continue
            ws = set(key.split())
            if any(len(ws & o) / max(1, len(ws | o)) >= 0.6 for o in seen_sets):
                continue
            if key in seen or CTA_START.search(s) or NOT_CLAIM.search(s) or s.isupper():
                continue
            if not compliance.check(s)["ok"]:
                continue
            seen.add(key)
            seen_sets.append(ws)
            out.append(s[:1].upper() + s[1:])
    # Drop a claim the words of a longer one already cover ("Soy wax" inside "A soy wax candle with ...").
    keep = []
    for c in out:
        k = " " + " ".join(re.findall(r"[a-z0-9]+", c.lower())) + " "
        if any(k.strip() != o.strip() and k in " " + o + " "
               for o in (" ".join(re.findall(r"[a-z0-9]+", x.lower())) for x in out)):
            continue
        keep.append(c)
    return keep[:limit]


def site_facts(html: str, url: str) -> dict:
    """Everything the first run needs from a business website, without AI."""
    p = seo._Parser()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001  broken HTML still gives partial results
        pass
    meta = p.meta
    ld = _business_ld(p.jsonld)
    links = _link_rels(html)
    name = business_name(ld, meta, p.title, url)
    logo = _logo_url(ld, p, url, links)
    photos, seen = [], set()
    for src in [meta.get("og:image", "")] + [i.get("src") for i in p.images
                                             if not product_import.SKIP_ALT.search(i.get("alt") or "")]:
        absu = product_import._usable(src, url)
        if absu and absu != logo and product_import._key(absu) not in seen:
            seen.add(product_import._key(absu))
            photos.append(absu)
    desc = str(ld.get("description") or meta.get("og:description") or meta.get("description") or "")
    desc = " ".join(desc.split())[:600]
    headings = [h for _, h in p.headings[:30]]
    h1 = next((h for t, h in p.headings if t == "h1" and 3 <= len(h) <= 60), "")
    text = " ".join(" ".join(p.text).split())[:9000]
    phone = ld.get("telephone") if isinstance(ld.get("telephone"), str) else ""
    facts = {"url": url, "name": name, "brand": name, "description": desc, "headings": headings, "text": text,
             "paragraphs": p.paragraphs[:40], "images": photos[:product_import.MAX_IMAGES],
             "own": photos[:product_import.MAX_IMAGES], "logo": logo, "colors": css_colors(html, meta),
             "area": _areas(ld), "phone": phone[:40], "headline": h1, "source": "jsonld" if ld else "page"}
    # Short description: the site's own (meta) description first; a meta line usually carries both services and area.
    meta_desc = " ".join(str(meta.get("description") or meta.get("og:description") or "").split())
    claim_facts = dict(facts, description=" ".join(x for x in (meta_desc, desc) if x))
    facts["claims"] = page_claims(claim_facts, name)
    facts["services"] = services_from(facts)
    return facts


def services_from(facts: dict) -> list:
    """Service names the site lists ("AC repair, furnace tune-ups and new system installs"), for keywords."""
    text = facts.get("description") or ""
    m = re.match(r"^(?:(?i:same[- ]day|fast|local|affordable|professional)\s+)?(.+?)"
                 r"(?:\s+(?:in|for|across|around|near|serving)\s+[A-Z].*)?$", text.split(".")[0])
    body = m.group(1) if m else ""
    parts = [x.strip(" .") for x in re.split(r",\s*|\s+and\s+|\s*&\s*", body) if x.strip(" .")]
    out = []
    for x in parts:
        x = re.sub(r"^(we offer|we do|offering|providing)\s+", "", x, flags=re.I)
        if 1 <= len(x.split()) <= 5 and not NOT_CLAIM.search(x) and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out[:8]


def read_site(url: str) -> dict:
    """Fetch a business website (SSRF-safe fetcher) and read it. Raises product_import.Blocked when the site refuses
    apps (never retried or disguised) and fetcher.FetchError for anything else."""
    fetcher.check_url(url)
    page = fetcher.fetch(url)
    if product_import.refused(page):
        raise product_import.Blocked(page["status"], "page")
    if page["status"] >= 400:
        raise fetcher.FetchError(f"The page answered {page['status']}.")
    if "html" not in (page["content_type"] or "").lower() and "<html" not in (page["text"] or "")[:2000].lower():
        raise fetcher.FetchError("That link isn't a web page.")
    return site_facts(page["text"], page["url"])


def download_logo(url: str):
    """(bytes, content type) for the logo; logos may be small (48 px and up). Raises fetcher.FetchError."""
    r = fetcher.fetch_bytes(url, product_import.MAX_IMAGE_BYTES)
    if r["status"] >= 400:
        raise fetcher.FetchError(f"The logo answered {r['status']}.")
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(r["data"]))
        fmt, (w, h) = (im.format or "").upper(), im.size
    except Exception:  # noqa: BLE001
        raise fetcher.FetchError("That logo isn't an image we can use.") from None
    ctype = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(fmt)
    if not ctype or min(w, h) < 48:
        raise fetcher.FetchError("That logo isn't an image we can use.")
    return r["data"], ctype


# ---------------------------------------------------------------- first ads without a product photo

# Image templates for a service. "photo": True needs a photo (from the site or uploaded); the others don't.
# {product} is the business (or service) name, {headline} the site's own headline or the name, {colors} the brand
# colours in plain words, {area} the service area.
TEMPLATES = [
    {"id": "svc-logo", "name": "Logo and headline", "line": "Your logo, one headline and a button. No photo needed",
     "tool": "ad_image", "aspect": "4:5", "preview": "svc-logo", "photo": False, "fits": "service",
     "brief": "Clean graphic ad for {product}, a local service business. A calm background in {colors}, the logo from "
              "the reference image shown once near the top, unchanged, and plenty of empty space. No people, no "
              "product. Headline: \"{headline}\". Button: \"Book a call\".",
     "audience": ""},
    {"id": "svc-photo", "name": "Your work, with a headline", "line": "A photo from your site with your headline",
     "tool": "ad_image", "aspect": "4:5", "preview": "svc-photo", "photo": True, "fits": "service",
     "brief": "Advertising image built on the reference photo from {product}'s website: keep its people, place and "
              "work exactly as they are, add a soft dark band at the bottom in {colors} so the words are easy to "
              "read. Headline: \"{headline}\". Button: \"Get a quote\".",
     "audience": ""},
    {"id": "svc-area", "name": "Local card", "line": "Your service area, big and clear. No photo needed",
     "tool": "ad_image", "aspect": "1:1", "preview": "svc-area", "photo": False, "fits": "service",
     "brief": "Simple local ad for {product}: a flat, friendly illustration of a neighbourhood street in {colors}, "
              "with a single map pin. No people, no real addresses. Headline: \"{product}, {area}\". Button: "
              "\"Book a visit\".",
     "audience": ""},
]
TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}


def template_vars(name: str, site: dict) -> dict:
    colors = [color_name(c) for c in (site.get("colors") or [])]
    colors = list(dict.fromkeys(colors))[:2]
    area = (site.get("area") or [])
    return {"headline": (site.get("headline") or name or "{product}")[:60],
            "colors": " and ".join(colors) if colors else "the brand's colours",
            "area": " and ".join(area[:2]) if area else "near you"}


def templates(name: str = "", site: dict | None = None) -> list:
    """The service templates with the site's headline, colours and area filled in ({product} stays for the form)."""
    v = template_vars(name, site or {})
    out = []
    for t in TEMPLATES:
        b = t["brief"].replace("{headline}", v["headline"]).replace("{colors}", v["colors"]).replace("{area}", v["area"])
        out.append(dict(t, brief=b))
    return out


def image_lines(product: str) -> list:
    """What the image model is told about the reference images for a service ad (catalog.image_ad_prompt)."""
    return [f"This ad is for {product}, a service business, not a packaged product.",
            "Any reference image is the business's own logo or a photo from its website. Keep a logo exactly as it is "
            "(shape, letters and colours) and show it once. Keep a photo's people, place and work real: don't change "
            "faces, add people or invent equipment. With no reference image, make a clean graphic design with no "
            "people and no product."]


# ---------------------------------------------------------------- the plan: keywords and lead steps

MATCHES = ("exact", "phrase", "broad")
NEGATIVES = ["jobs", "careers", "salary", "training", "diy", "how to"]


def clean_keywords(items) -> list:
    """The model's keyword list, cleaned: 10 to 20 terms with a match type, compliance-checked."""
    out, seen = [], set()
    for it in items or []:
        if isinstance(it, str):
            it = {"term": it}
        if not isinstance(it, dict):
            continue
        term = " ".join(re.findall(r"[A-Za-z0-9'&+-]+", str(it.get("term") or "")))[:80].lower()
        match = it.get("match") if it.get("match") in MATCHES else "phrase"
        if term and term not in seen and len(term.split()) <= 7 and compliance.check(term)["ok"]:
            seen.add(term)
            out.append({"term": term, "match": match})
    return out[:20]


def starter_keywords(brand: dict, brief: dict, services=None, area=None) -> list:
    """A first keyword list from the services and the service area, when the model gave none. Exact and phrase
    match for "service + city" and "service near me"; one broad term per service."""
    services = [s for s in (services or []) if s] or [brief.get("offer") or brand.get("product") or ""]
    services = [s.lower().strip() for s in services if s and len(s.split()) <= 5][:5]
    raw = (brief.get("area") or "") if not area else ", ".join(area)
    raw = re.sub(r"(?i)\b(within|up to|about)?\s*\d+\s*(mi|km|miles?|kilometers?)\b\s*(of|around|from)?", ",", raw)
    places = [a.strip(" .") for a in re.split(r",|\band\b|/|;", raw)
              if a.strip(" .") and re.search(r"[A-Za-z]{3}", a) and len(a.split()) <= 3][:3]
    out = []
    for s in services:
        for pl in places[:2]:
            out.append({"term": f"{s} {pl.lower()}", "match": "exact"})
        out.append({"term": f"{s} near me", "match": "phrase"})
        out.append({"term": s, "match": "broad" if len(s.split()) > 1 else "phrase"})
        if len(services) <= 2:   # few services: the other ways people search for one
            out += [{"term": f"{s} cost", "match": "phrase"}, {"term": f"{s} company", "match": "phrase"}]
    if places:
        biz = (brand.get("name") or "").strip()
        if biz:
            out.append({"term": biz.lower(), "match": "exact"})
    return clean_keywords(out)


LEAD_STEPS = [
    {"title": "Publish the lead page", "platform": "all", "link": "pages",
     "detail": "One page in Pages with your headline, 1 photo or your logo, and the quote form. The ads point here."},
    {"title": "Add the quote form to it", "platform": "all", "link": "forms",
     "detail": "Name, phone or email, and what they need. Ask only for what you'll use; add it to the page as a block."},
    {"title": "Turn on the follow-up flow", "platform": "all", "link": "flows",
     "detail": "A flow that starts when the form is sent: a thank-you email or text right away and a task for you "
               "to call back."},
]


def lead_steps(checklist: list, service: bool) -> list:
    """A leads plan's checklist with the page, form and follow-up steps (UX-14). For a service the planner's own
    "landing page and form" item is replaced by these three; the lead-magnet item stays only for products."""
    def has(item, words):
        t = (item.get("title", "") + " " + item.get("detail", "")).lower()
        return any(w in t for w in words)
    keep = [i for i in checklist if not (i.get("title") in ("Build the landing page and form",) or
                                         (service and has(i, ("lead magnet",)) and i.get("platform") == "all"
                                          and i.get("title") == "Make the lead magnet"))]
    return ([dict(s) for s in LEAD_STEPS] + keep)[:24]


SERVICE_PROMPT = """This is a local service business: it sells appointments, visits or quotes, not a cart. With the goal
Leads, plan Google Search for people already searching (with a keyword list in finish.search_keywords: 10 to 20 terms
built from the services and the service area, each with a match type: exact or phrase for "service + city" and
"service near me", broad only for longer terms) plus Meta lead ads aimed at the service area. The ads send people to a
landing page with a short quote form; a follow-up flow answers each lead. Never promise response times, prices or
results the brief doesn't state."""

KEYWORDS_PROMPT = """Google is one of the platforms: put a Google Search keyword list in finish.search_keywords (10 to 20
terms a buyer would type, each with a match type exact, phrase or broad)."""

KEYWORDS_SCHEMA = {"type": "array", "description": "Google Search keywords (10-20), only when Google is a platform",
                   "items": {"type": "object", "properties": {
                       "term": {"type": "string"}, "match": {"type": "string", "enum": list(MATCHES)}},
                       "required": ["term"]}}

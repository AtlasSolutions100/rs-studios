"""Import a product from its page: name, description, price, suggested claims
and candidate photos, so setup takes one link instead of a form.

The page is fetched with the SSRF-safe fetcher (fetcher.py). What the page
states plainly (JSON-LD Product, Open Graph, meta tags, <img>) is read for
free; the writing model then tidies it into a product and proposes claims
*taken from the page's own wording*. Suggested claims are never approved
here: they land in products.claims_pending and the member approves them one
by one. Photos are only candidates until the member picks them, and only the
candidates found on the page can be downloaded (no arbitrary URLs).
"""
import io
import json
import re
from urllib.parse import urljoin, urlsplit

import compliance
import fetcher
import llm
import seo

MAX_IMAGES = 16
MAX_IMAGE_BYTES = 15 * 1024 * 1024
TOKENS_IN, TOKENS_OUT = 6000, 900
SKIP_IMG = re.compile(r"(sprite|logo|icon|favicon|pixel|spacer|badge|payment|flag|avatar|rating|star|loader|"
                      r"placeholder|blank|tracking|1x1|award|winner|certif|trustpilot|trust[-_]?badge|guarantee|"
                      r"as[-_]?seen|featured[-_]?in|infographic|percent|before[-_]?(and[-_]?)?after|before[-_]|"
                      r"[-_]after(?![a-z])|testimonial|swatch|klarna|afterpay|paypal|shop[-_]?pay|visa|mastercard|amex|"
                      r"(?<![a-z])(banner|chart|graph|stats?|results?|reviews?|social|arrow|chevron|ugc)(?![a-z]))", re.I)
# Alt text that marks a graphic rather than a product photo.
SKIP_ALT = re.compile(r"\b(badge|award|winner|seal|certified|logo|icon|before|after|results?|review|rating|stars?|"
                      r"testimonial|as seen|featured in|infographic|chart|percent|%)\b", re.I)


def estimate() -> int:
    """Most the AI part of an import can cost (fast model)."""
    return llm.estimate_credits(TOKENS_IN, TOKENS_OUT, model_id=llm.fast_model())


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _ld_products(blocks):
    """Every schema.org Product (or ProductGroup) in the page's JSON-LD."""
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            ts = t if isinstance(t, list) else [t]
            if any(str(x) in ("Product", "ProductGroup", "IndividualProduct") for x in ts):
                out.append(o)
            for k in ("@graph", "mainEntity", "itemListElement", "hasVariant"):
                if k in o:
                    walk(o[k])
    for b in blocks:
        walk(b)
    return out


def _img_urls(v):
    vals = v if isinstance(v, list) else [v]
    out = []
    for x in vals:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and (x.get("url") or x.get("contentUrl")):
            out.append(x.get("url") or x.get("contentUrl"))
    return out


def _price(ld):
    offers = ld.get("offers")
    for o in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(o, dict):
            continue
        p = o.get("price") or o.get("lowPrice")
        if p is None and isinstance(o.get("priceSpecification"), dict):
            p = o["priceSpecification"].get("price")
        if p not in (None, ""):
            cur = o.get("priceCurrency") or ""
            return f"{'$' if cur in ('USD', '') else cur + ' '}{p}".strip()
    return ""


def extract(html: str, url: str) -> dict:
    """Facts the page states, without any AI: name, description, price,
    brand, candidate image URLs (absolute, deduplicated) and the page text."""
    p = seo._Parser()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001
        pass
    meta = p.meta
    lds = _ld_products(p.jsonld)
    ld = lds[0] if lds else {}
    own, seen = [], set()
    # The product's own photos come first: the JSON-LD Product's images (and its
    # variants'), then the page's share image. Other <img> tags on the page are
    # often other products (cross-sells, "goes well with"), award badges, stat
    # graphics or before/after photos: they're kept apart as "other images".
    ld_imgs = _img_urls(ld.get("image"))
    for v in (ld.get("hasVariant") or []) if isinstance(ld.get("hasVariant"), list) else []:
        if isinstance(v, dict):
            ld_imgs += _img_urls(v.get("image"))
    for src in ld_imgs + [meta.get("og:image", ""), meta.get("og:image:secure_url", ""), meta.get("twitter:image", "")]:
        absu = _usable(src, url)
        if absu and _key(absu) not in seen:
            seen.add(_key(absu))
            own.append(absu)
    others = []
    for i in p.images:
        absu = _usable(i.get("src"), url)
        if not absu or _key(absu) in seen or SKIP_ALT.search(i.get("alt") or ""):
            continue
        seen.add(_key(absu))
        others.append(absu)
    imgs = own + others
    brand = ld.get("brand")
    brand = (brand.get("name") if isinstance(brand, dict) else brand) or meta.get("og:site_name", "")
    price = _price(ld) if ld else ""
    if not price and meta.get("product:price:amount"):
        cur = meta.get("product:price:currency", "")
        price = f"{'$' if cur in ('USD', '') else cur + ' '}{meta['product:price:amount']}"
    name = ld.get("name") or meta.get("og:title") or " ".join(p.title.split())
    desc = ld.get("description") or meta.get("og:description") or meta.get("description", "")
    text = " ".join(" ".join(p.text).split())
    source = "jsonld" if ld else "og" if (meta.get("og:title") or meta.get("og:description")) else "page"
    return {"url": url, "name": str(name or "").strip()[:200], "description": re.sub(r"\s+", " ", str(desc or "")).strip()[:1500],
            "price": str(price)[:60], "brand": str(brand or "")[:80], "images": imgs[:MAX_IMAGES],
            "own": own[:MAX_IMAGES],
            "headings": [h for _, h in p.headings[:30]], "text": text[:9000], "source": source}


def _usable(src, base) -> str:
    """An absolute http(s) image URL worth offering, or "" (data URIs, SVG/GIF
    icons, logos, badges and other graphics by file name)."""
    src = (src or "").strip()
    if not src or src.startswith("data:") or src.lower().split("?")[0].endswith((".svg", ".gif", ".ico")):
        return ""
    absu = urljoin(base, src)
    if urlsplit(absu).scheme not in ("http", "https") or SKIP_IMG.search(absu.rsplit("/", 1)[-1].split("?")[0]):
        return ""
    return absu


def _key(u: str) -> str:
    """Same picture at another size (Shopify's _400x suffix, ?width=) counts once."""
    path = urlsplit(u)
    name = re.sub(r"_(\d+x\d*|\d*x\d+|pico|icon|thumb|small|compact|medium|large|grande|master)(?=\.\w+$)", "",
                  path.path.rsplit("/", 1)[-1], flags=re.I)
    return (path.netloc + path.path.rsplit("/", 1)[0] + "/" + name).lower()


SHOPIFY_PATH = re.compile(r"/products/([A-Za-z0-9][A-Za-z0-9_.%-]{0,200})/?$")

# ---------------------------------------------------------------- stores that refuse apps
# Some stores' bot protection refuses our server. We never try to get past it:
# no other user-agent, no other address, no headless browser, no retry. The
# first refusal from a store ends the import and the member adds the details
# by hand (a 30-second form, prefilled with what we know).
REFUSED = (401, 403, 429)
CHALLENGE_TITLE = re.compile(r"<title[^>]*>\s*(just a moment|attention required|access denied|are you a (robot|human)|"
                             r"verify(ing)? you are (a )?human|pardon our interruption|security check|"
                             r"one more step|robot check|request blocked|bot (check|protection))", re.I)
CHALLENGE_MARK = re.compile(r'id="challenge-(form|running|stage)"|captcha-delivery\.com|px-captcha|'
                            r'/_Incapsula_Resource|cf-browser-verification|g-recaptcha', re.I)


class Blocked(fetcher.FetchError):
    """The store refused us (403/429/a bot challenge). Not retried."""

    def __init__(self, status: int, method: str = "page"):
        self.status, self.method = status, method
        super().__init__("This store doesn't let apps read its pages automatically.")


def refused(r: dict) -> bool:
    """True when a store's answer is its bot protection saying no: 401/403/429,
    or a challenge page (any status) instead of the product."""
    if r["status"] in REFUSED:
        return True
    head = (r.get("text") or "")[:20000]
    if CHALLENGE_TITLE.search(head):
        return True
    # A challenge page is small and has no product in it; a real product page
    # that merely loads a captcha script for its review form is not one.
    return (r["status"] >= 400 or len(head) < 15000) and bool(CHALLENGE_MARK.search(head)) \
        and "application/ld+json" not in head and 'property="og:title"' not in head


def domain(url: str) -> str:
    """The store's host name for the log ("" when the link has none)."""
    url = (url or "").strip()
    if "://" not in url:
        url = "https://" + url
    try:
        host = (urlsplit(url).hostname or "").strip(".").lower()
    except ValueError:
        host = ""
    return (host[4:] if host.startswith("www.") else host)[:120]


COMMON_SUB = {"www", "shop", "store", "m", "us", "en", "eu", "uk", "ca", "au"}


def brand_guess(url: str) -> str:
    """A first guess at the brand from the store's domain: glow-co.com -> "Glow Co",
    shop.hearthcandles.com -> "Hearthcandles". The member corrects it."""
    host = domain(url)
    if not host or re.fullmatch(r"[\d.:]+|\[[0-9a-f:]+\]", host):
        return ""
    parts = [p for p in host.split(".") if p]
    if parts and parts[-1] in ("com", "net", "org", "co", "io", "store", "shop", "us", "uk", "ca", "au", "de", "fr",
                               "nl", "eu", "ai", "app", "life", "studio", "beauty", "health", "inc", "biz", "info"):
        parts = parts[:-1]
    if len(parts) > 1 and parts[-1] in ("co", "com", "org", "net"):      # .co.uk, .com.au
        parts = parts[:-1]
    parts = [p for p in parts if p not in COMMON_SUB] or parts
    if not parts:
        return ""
    name = parts[0] if parts[-1] == "myshopify" else parts[-1]
    words = [w for w in re.split(r"[-_]+", name) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)[:80]


def name_guess(url: str) -> str:
    """The product's name from its link: /products/vitamin-c-serum -> "Vitamin C Serum"."""
    try:
        path = urlsplit(url if "://" in (url or "") else "https://" + (url or "")).path
    except ValueError:
        return ""
    segs = [s for s in path.split("/") if s]
    if not segs:
        return ""
    slug = segs[-1]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    if segs[:-1] and segs[-2] in ("p", "dp") and re.fullmatch(r"[A-Z0-9]{8,12}", slug):
        slug = segs[-3] if len(segs) > 2 else ""        # amazon-style /<name>/dp/<asin>
    from urllib.parse import unquote
    slug = unquote(slug)
    if not re.search(r"[A-Za-z]{3}", slug) or re.fullmatch(r"[0-9a-f-]{16,}", slug, re.I):
        return ""
    words = [w for w in re.split(r"[-_+\s]+", slug) if w and not re.fullmatch(r"\d{5,}", w)]
    out = " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)
    return out[:80]


# ---------------------------------------------------------------- the store's published product data
def shopify_endpoints(url: str) -> list:
    """Shopify's documented product endpoints for a product link
    (<product-url>.js, then <product-url>.json), or [] when the path isn't
    /products/<handle>. The query string is dropped (it's the variant)."""
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return []
    m = SHOPIFY_PATH.search(parts.path)
    if not m or m.group(1).endswith((".json", ".js")) or parts.scheme not in ("http", "https"):
        return []
    base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    return [base + ".js", base + ".json"]


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    import html as _html
    s = _html.unescape(s)
    return "\n".join(" ".join(line.split()) for line in s.splitlines() if line.strip())


def shopify_facts(text: str, url: str, ctype: str = "") -> dict | None:
    """Facts (the same shape as extract()) from a Shopify product .js or .json
    body, or None when it isn't one. .js prices are in cents, .json in units."""
    if ctype and not re.search(r"json|javascript", ctype, re.I):
        return None
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cents = "product" not in data
    prod = data.get("product") if not cents else data
    if not isinstance(prod, dict) or not prod.get("title") or not isinstance(prod.get("title"), str):
        return None
    vs = [v for v in prod.get("variants") or [] if isinstance(v, dict)]
    raw = prod.get("price") if cents else None
    if raw is None and vs:
        raw = vs[0].get("price")
    price = ""
    try:
        if raw not in (None, ""):
            n = float(raw) / (100 if cents and isinstance(raw, int) else 1)
            price = f"${n:,.2f}".replace(".00", "") if n else ""
    except (TypeError, ValueError):
        price = ""
    imgs = []
    for im in (prod.get("images") or []):
        src = im if isinstance(im, str) else im.get("src") if isinstance(im, dict) else None
        if isinstance(src, str) and src.startswith(("http://", "https://", "//")):
            imgs.append("https:" + src if src.startswith("//") else src)
    desc_html = prod.get("description") if cents else prod.get("body_html")
    body = _strip_html(desc_html if isinstance(desc_html, str) else "")
    first = re.split(r"(?<=[.!?])\s|\n", body, maxsplit=2)
    desc = " ".join(first[:2]).strip()[:600] if body else ""
    tags = prod.get("tags")
    tags = tags if isinstance(tags, list) else [t.strip() for t in str(tags or "").split(",") if t.strip()]
    own, seen = [], set()
    for u in imgs:
        absu = _usable(u, url)
        if absu and _key(absu) not in seen:
            seen.add(_key(absu))
            own.append(absu)
    return {"url": url, "name": prod["title"].strip()[:200], "description": desc, "price": price[:60],
            "brand": str(prod.get("vendor") or "")[:80], "images": own[:MAX_IMAGES], "own": own[:MAX_IMAGES],
            "headings": [], "text": body[:9000], "shop_tags": [str(t) for t in tags[:5]], "source": "shopify-json"}


def merge(page: dict, shop: dict) -> dict:
    """The page's facts with the store's product data filled in: Shopify's
    title, vendor, photos and description text win where the page is thin."""
    out = dict(page)
    # A page's JSON-LD name is the product's own; a bare <title>/og:title often carries the store's slogan.
    out["name"] = (page.get("name") if page.get("source") == "jsonld" else "") or shop["name"] or page.get("name", "")
    out["brand"] = page.get("brand") or shop["brand"]
    out["price"] = page.get("price") or shop["price"]
    out["description"] = page.get("description") or shop["description"]
    out["text"] = (shop["text"] + " " + page.get("text", "")).strip()[:9000]
    prefer_images(out, shop["own"])
    out["source"] = "shopify-json"
    return out


def text_facts(text: str, url: str = "", name: str = "") -> dict:
    """Facts from product page text the member pasted (the store wouldn't let
    us read it). Only a first guess at the name and price; the AI step does
    the rest, exactly as for a page we read ourselves."""
    text = (text or "").replace("\r", "")[:20000]
    lines = [" ".join(ln.split()) for ln in text.split("\n") if ln.strip()]
    def ok(ln):
        return 3 <= len(ln) <= 80 and re.search(r"[A-Za-z]{3}", ln) \
            and not re.search(r"[$€£]|\b(cart|menu|sign in|log in|account|search|shipping|reviews?)\b", ln, re.I)
    # Product pages put the name just above the price; failing that, the first plausible line.
    at = next((i for i, ln in enumerate(lines) if re.search(r"[$€£]\s?\d", ln)), None)
    above = [ln for ln in (lines[:at] if at else []) if ok(ln)]
    guess = name or (above[-1] if above else next((ln for ln in lines if ok(ln)), ""))
    m = re.search(r"(?:US\s?)?([$€£])\s?(\d{1,5}(?:[.,]\d{2})?)", text)
    price = f"{m.group(1)}{m.group(2)}" if m else ""
    body = " ".join(lines)
    return {"url": url, "name": guess[:200], "description": "", "price": price, "brand": "", "images": [], "own": [],
            "headings": [], "text": body[:9000], "source": "manual"}


def method_of(facts: dict) -> str:
    """Which of the page's structured data the import mostly stood on."""
    return facts.get("source") or "page"


def log_attempt(conn, ws_id, url: str, method: str, status: str, http: int | None = None, detail: str = ""):
    """One row per import try, for the admin system card (domain, how, outcome)."""
    import db
    conn.execute("INSERT INTO import_attempts (workspace_id, domain, method, status, http_status, detail, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (ws_id, domain(url) or "(none)", method, status, http, (detail or "")[:200],
                                           db.now()))
    conn.commit()


def shopify_json_url(url: str, html: str) -> str:
    """The store's own product JSON (/products/<handle>.json) for a Shopify
    product page, or "" when the page isn't one."""
    parts = urlsplit(url)
    m = SHOPIFY_PATH.search(parts.path)
    if not m or not re.search(r"cdn\.shopify\.com|Shopify\.(shop|theme)|shopify-section", html or "", re.I):
        return ""
    handle = m.group(1)
    if handle.endswith((".json", ".js")):
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}.json"


def shopify_images(text: str) -> list:
    """Image URLs from a Shopify /products/<handle>.json body (in its order)."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    prod = data.get("product") if isinstance(data, dict) else None
    if not isinstance(prod, dict):
        return []
    out = []
    for im in prod.get("images") or []:
        src = im.get("src") if isinstance(im, dict) else im if isinstance(im, str) else None
        if isinstance(src, str) and src.startswith(("http://", "https://", "//")):
            out.append("https:" + src if src.startswith("//") else src)
    return out


def prefer_images(facts: dict, first: list) -> dict:
    """Put the product's own images (e.g. from its Shopify JSON) at the front."""
    seen, own = set(), []
    for u in list(first) + list(facts.get("own") or []):
        absu = _usable(u, facts["url"])
        if absu and _key(absu) not in seen:
            seen.add(_key(absu))
            own.append(absu)
    others = [u for u in facts.get("images") or [] if _key(u) not in seen]
    facts["own"] = own[:MAX_IMAGES]
    facts["images"] = (own + others)[:MAX_IMAGES]
    return facts


SYSTEM = """You turn a product web page into a product record for an ad tool. Use only what the page says.
- name: the product's own name, without the store name or slogans (at most 80 characters).
- description: one or two plain sentences on what it is and who it's for, in the page's words (at most 300 characters).
- price: as shown on the page (e.g. "$29" or "$59 / month"), or empty.
- offer: a bundle or free extra if the page states one, else empty. Never discounts, coupons or free shipping.
- claims: 3-8 short product facts the page states word for word or nearly so (ingredients, size, servings, materials,
  how to use, certifications the page shows). No health, disease, weight-loss, income or results claims, nothing the
  page doesn't say, no superlatives you can't point to. These are only suggestions; the seller approves them.
- audience: who the page seems to sell to, in a few words, or empty.
- tags: 2-5 lowercase category words.
- brand: the brand name if the page shows one."""

SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string"}, "description": {"type": "string"}, "price": {"type": "string"},
    "offer": {"type": "string"}, "claims": {"type": "array", "items": {"type": "string"}},
    "audience": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
    "brand": {"type": "string"}}, "required": ["name", "description", "claims"]}


def suggest(facts: dict, usage: llm.Usage | None = None) -> dict:
    """The AI tidy-up. Raises llm.LLMError."""
    prompt = (f"Page: {facts['url']}\nTitle/name found: {facts['name']}\nBrand found: {facts['brand']}\n"
              f"Price found: {facts['price']}\nDescription found: {facts['description']}\n"
              f"Headings: {' | '.join(facts['headings'])}\n\nPage text:\n{facts['text'][:7000]}")
    return llm.structured(system=SYSTEM, prompt=prompt, schema=SCHEMA, name="submit_product", max_tokens=TOKENS_OUT,
                          usage=usage, temperature=0.2, model_id=llm.fast_model())


def _supported(claim: str, text: str) -> bool:
    """A suggested claim must lean on the page's words: most of its longer
    words appear in the page text."""
    words = [w for w in re.findall(r"[a-z0-9]+", claim.lower()) if len(w) > 3]
    if not words:
        return False
    page = text.lower()
    return sum(1 for w in words if w in page) / len(words) >= 0.6


def build(facts: dict, ai: dict | None) -> dict:
    """{form, pending, import} for products.create_product()."""
    ai = ai or {}
    page_text = " ".join([facts["text"], facts["description"], " ".join(facts["headings"])])
    claims = []
    for c in ai.get("claims") or []:
        c = " ".join(str(c).split())[:160]
        if c and c not in claims and compliance.check(c)["ok"] and _supported(c, page_text):
            claims.append(c)
    offer = str(ai.get("offer") or "")[:200]
    if offer and not compliance.check(offer)["ok"]:
        offer = ""
    desc = str(ai.get("description") or facts["description"])[:600]
    if not compliance.check(desc)["ok"]:
        desc = ""
    form = {"name": str(ai.get("name") or facts["name"] or "")[:80].strip() or "Imported product",
            "description": desc, "price": str(ai.get("price") or facts["price"])[:60], "offer": offer,
            "audience": str(ai.get("audience") or "")[:300] if compliance.check(str(ai.get("audience") or ""))["ok"] else "",
            "url": facts["url"][:500],
            "tags": ", ".join(str(t) for t in (ai.get("tags") or facts.get("shop_tags") or [])[:5])}
    import products as _products
    form["category"] = _products.guess_category(" ".join([form["tags"], form["name"], form["description"]]))
    return {"form": form, "pending": claims[:12], "brand": str(ai.get("brand") or facts["brand"] or "")[:80],
            "import": {"url": facts["url"], "images": facts["images"], "own": facts.get("own") or [], "taken": []}}


def download_image(url: str) -> tuple[bytes, str]:
    """(bytes, content type) for one candidate image; checks it really is a
    JPEG, PNG or WebP. Raises fetcher.FetchError."""
    r = fetcher.fetch_bytes(url, MAX_IMAGE_BYTES)
    if r["status"] >= 400:
        raise fetcher.FetchError(f"The image answered {r['status']}.")
    data = r["data"]
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "").upper()
        w, h = im.size
    except Exception:  # noqa: BLE001
        raise fetcher.FetchError("That isn't an image we can use.") from None
    ctype = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(fmt)
    if not ctype:
        raise fetcher.FetchError("Only JPEG, PNG or WebP images.")
    if min(w, h) < 200:
        raise fetcher.FetchError("That image is too small to use in an ad.")
    return data, ctype


def dumps(x) -> str:
    return json.dumps(x)

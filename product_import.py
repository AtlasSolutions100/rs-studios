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
                      r"placeholder|blank|tracking|1x1)", re.I)


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
    imgs = []
    for src in _img_urls(ld.get("image")) + [meta.get("og:image", ""), meta.get("og:image:secure_url", ""),
                                            meta.get("twitter:image", "")] + [i["src"] for i in p.images]:
        src = (src or "").strip()
        if not src or src.startswith("data:") or src.lower().split("?")[0].endswith((".svg", ".gif", ".ico")):
            continue
        absu = urljoin(url, src)
        if urlsplit(absu).scheme not in ("http", "https") or SKIP_IMG.search(absu.rsplit("/", 1)[-1]):
            continue
        if absu not in imgs:
            imgs.append(absu)
    brand = ld.get("brand")
    brand = (brand.get("name") if isinstance(brand, dict) else brand) or meta.get("og:site_name", "")
    price = _price(ld) if ld else ""
    if not price and meta.get("product:price:amount"):
        cur = meta.get("product:price:currency", "")
        price = f"{'$' if cur in ('USD', '') else cur + ' '}{meta['product:price:amount']}"
    name = ld.get("name") or meta.get("og:title") or " ".join(p.title.split())
    desc = ld.get("description") or meta.get("og:description") or meta.get("description", "")
    text = " ".join(" ".join(p.text).split())
    return {"url": url, "name": str(name or "").strip()[:200], "description": re.sub(r"\s+", " ", str(desc or "")).strip()[:1500],
            "price": str(price)[:60], "brand": str(brand or "")[:80], "images": imgs[:MAX_IMAGES],
            "headings": [h for _, h in p.headings[:30]], "text": text[:9000]}


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
            "url": facts["url"][:500], "tags": ", ".join(str(t) for t in (ai.get("tags") or [])[:5])}
    return {"form": form, "pending": claims[:12], "brand": str(ai.get("brand") or facts["brand"] or "")[:80],
            "import": {"url": facts["url"], "images": facts["images"], "taken": []}}


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

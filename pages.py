"""Landing pages built from blocks, and the rules that keep them safe.

Members never write HTML. Every block is a few plain-text fields; the public
template escapes all of it. Links must be http(s), mailto: or tel:. Embeds are
rebuilt from a URL on a short allow-list of providers (we construct the iframe
src ourselves). Media comes only from the workspace's own Campaignify library.
Public pages carry a strict Content-Security-Policy with no scripts at all.
"""
import re
from urllib.parse import parse_qs, urlsplit

BLOCKS = {
    "hero": {"name": "Hero", "fields": [("headline", "Headline", 120), ("subhead", "Subheadline", 300),
                                         ("button_text", "Button text", 40), ("button_url", "Button link", 500),
                                         ("media", "Image or video", 0)]},
    "media": {"name": "Image or video", "fields": [("media", "From your library", 0), ("caption", "Caption", 200)]},
    "text": {"name": "Text", "fields": [("heading", "Heading", 120), ("body", "Text", 3000)]},
    "benefits": {"name": "Benefits", "fields": [("heading", "Heading", 120), ("items", "One benefit per line", 1500)]},
    "faq": {"name": "FAQ", "fields": [("heading", "Heading", 120),
                                       ("items", "Question on one line, answer on the next, blank line between", 4000)]},
    "cta": {"name": "Call to action", "fields": [("headline", "Headline", 120), ("body", "Text", 400),
                                                  ("button_text", "Button text", 40), ("button_url", "Button link", 500)]},
    "embed": {"name": "Embed", "fields": [("url", "YouTube, Vimeo, Loom, Calendly or Spotify link", 500),
                                           ("caption", "Caption", 200)]},
}
MAX_BLOCKS = 30
SLUG_RX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,58})[a-z0-9]$")
RESERVED = {"app", "admin", "api", "login", "signup", "logout", "static", "media", "webhooks", "p", "l", "f",
            "features", "privacy", "terms", "help", "support", "campaignify", "billing", "www", "mail", "new"}
HEX_RX = re.compile(r"^#[0-9a-fA-F]{6}$")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60].strip("-")


def slug_ok(slug: str) -> bool:
    return bool(SLUG_RX.match(slug or "")) and slug not in RESERVED and "--" not in slug


def safe_url(u: str) -> str:
    """http(s), mailto: or tel: only; anything else becomes ''."""
    u = (u or "").strip()
    if not u:
        return ""
    low = u.lower()
    if low.startswith(("mailto:", "tel:")):
        return u if re.match(r"^(mailto:[^\s<>\"']+|tel:[+0-9() .-]+)$", u, re.I) else ""
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", low)
    if scheme and scheme.group(1) not in ("http", "https"):
        return ""          # javascript:, data:, vbscript:, file: …
    if "://" not in u and not u.startswith("/"):
        u = "https://" + u
    if u.startswith("/") and not u.startswith("//"):
        return u
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.hostname or re.search(r"[\s<>\"']", u):
        return ""
    return u


def embed_src(url: str) -> str | None:
    """An iframe src for an allow-listed provider, built by us. None otherwise."""
    u = safe_url(url)
    if not u or not u.startswith("https://"):
        return None
    p = urlsplit(u)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = p.path
    if host in ("youtube.com", "youtu.be", "youtube-nocookie.com"):
        vid = None
        if host == "youtu.be":
            vid = path.strip("/").split("/")[0]
        elif path.startswith("/watch"):
            vid = (parse_qs(p.query).get("v") or [None])[0]
        elif path.startswith(("/shorts/", "/embed/", "/live/")):
            vid = path.split("/")[2] if len(path.split("/")) > 2 else None
        if vid and re.match(r"^[A-Za-z0-9_-]{6,15}$", vid):
            return f"https://www.youtube-nocookie.com/embed/{vid}"
    if host in ("vimeo.com", "player.vimeo.com"):
        m = re.search(r"/(?:video/)?(\d{5,12})", path)
        if m:
            return f"https://player.vimeo.com/video/{m.group(1)}"
    if host == "loom.com":
        m = re.match(r"^/(?:share|embed)/([a-f0-9]{16,40})", path)
        if m:
            return f"https://www.loom.com/embed/{m.group(1)}"
    if host == "calendly.com":
        m = re.match(r"^/([A-Za-z0-9_-]{2,60})(?:/([A-Za-z0-9_-]{1,80}))?/?$", path)
        if m:
            return "https://calendly.com/" + m.group(1) + (f"/{m.group(2)}" if m.group(2) else "")
    if host == "open.spotify.com":
        m = re.match(r"^/(?:embed/)?(track|album|playlist|episode|show)/([A-Za-z0-9]{10,40})", path)
        if m:
            return f"https://open.spotify.com/embed/{m.group(1)}/{m.group(2)}"
    return None


FRAME_SOURCES = "https://www.youtube-nocookie.com https://player.vimeo.com https://www.loom.com https://calendly.com " \
                "https://open.spotify.com"


def clean_block(kind: str, data: dict, media_ok) -> dict | None:
    """Validate one block from form data. media_ok(output_id) says whether the
    output belongs to this workspace."""
    spec = BLOCKS.get(kind)
    if not spec:
        return None
    out = {"type": kind}
    for key, _label, limit in spec["fields"]:
        val = data.get(key)
        if key == "media":
            try:
                mid = int(val or 0)
            except (TypeError, ValueError):
                mid = 0
            out["media"] = mid if mid and media_ok(mid) else None
        elif key.endswith("url"):
            out[key] = safe_url(str(val or "")[:limit])
        else:
            out[key] = str(val or "").replace("\r\n", "\n").strip()[:limit]
    if kind == "embed":
        out["src"] = embed_src(out.get("url", ""))
    return out


def faq_items(text: str) -> list[tuple[str, str]]:
    out = []
    for chunk in re.split(r"\n\s*\n", text or ""):
        lines = [ln.strip() for ln in chunk.strip().splitlines() if ln.strip()]
        if lines:
            out.append((lines[0], " ".join(lines[1:])))
    return out[:20]


def lines(text: str) -> list[str]:
    return [ln.strip(" -•*\t") for ln in (text or "").splitlines() if ln.strip(" -•*\t")][:20]


def page_text(blocks: list[dict]) -> str:
    """All member text on a page (for the compliance checks)."""
    parts = []
    for b in blocks:
        for k, v in b.items():
            if isinstance(v, str) and k not in ("type", "src") and not k.endswith("url"):
                parts.append(v)
    return "\n".join(parts)


def media_ids(blocks: list[dict]) -> set[int]:
    return {b["media"] for b in blocks if b.get("media")}


def starter_blocks(brand: dict) -> list[dict]:
    product = brand.get("product") or "Your product"
    return [
        {"type": "hero", "headline": product, "subhead": "One sentence on who it's for and what it does for them.",
         "button_text": "Get yours", "button_url": "", "media": None},
        {"type": "benefits", "heading": "Why people pick it", "items": "First benefit\nSecond benefit\nThird benefit"},
        {"type": "faq", "heading": "Questions", "items": "How do I use it?\nShort, direct answer.\n\nWhat's in it?\n"
                                                         "Only what's printed on the label."},
        {"type": "cta", "headline": "Ready when you are", "body": "", "button_text": "Get yours", "button_url": ""},
    ]


CSP = ("default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; form-action 'self'; base-uri 'none'; "
       f"frame-src {FRAME_SOURCES}; frame-ancestors 'none'")

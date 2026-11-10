"""The words set on a keyframe-first video, as pictures ffmpeg lays over it (m15, keyframe_video.py). No model and no
network: Pillow, in the brand kit's colours and font (textlayer.py), inside each shape's safe area.

    caption_chunks()   a beat's spoken line cut into short chunks, timed evenly (by words) within the beat
    caption_png()      one chunk as a full-frame transparent picture: white type on a dark panel, low in the safe area
                       (high, when the presenter's small picture sits low over a screenshot)
    tag_png()          a small pill: a step's tag ("Step 1") top left, a standing label ("AI presenter") top right
    end_card()         the end card: the member's real product photo on the brand colour, the product's name, the
                       saved price or offer as a badge when the recipe asks for one (never typed fresh), one button
                       and the logo, set by textlayer.render like any static's words

The video model is never asked to draw a word: every prompt says "no on-screen text, no subtitles".
"""
import os
import re

from PIL import Image, ImageDraw

import textlayer

CAPTION_WORDS = 4                  # at most this many words on screen at once
CAPTION_LEAD, CAPTION_TAIL = 0.12, 0.18     # seconds of a beat left clear before the first chunk and after the last
END_CARD_SECONDS = (1.5, 2.5)
CTA_DEFAULT = {"app_service": "Try it free", "": "Shop now"}


def caption_chunks(line: str, seconds: float, max_words: int = CAPTION_WORDS) -> list:
    """[{"text", "start", "end"}]: the line in chunks of at most `max_words` words (a chunk also ends at a comma or
    a full stop once it has two words), each on screen for its share of the beat by its number of words."""
    words = " ".join(str(line or "").split()).split()
    if not words or seconds <= 0:
        return []
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or (len(cur) >= 2 and re.search(r"[,.!?;:]$", w)):
            chunks.append(cur)
            cur = []
    if cur:
        if chunks and len(cur) == 1 and len(chunks[-1]) < max_words + 1:
            chunks[-1] = chunks[-1] + cur          # never one word left alone at the end
        else:
            chunks.append(cur)
    lead = min(CAPTION_LEAD, seconds * 0.1)
    span = max(0.1, seconds - lead - min(CAPTION_TAIL, seconds * 0.1))
    total, t, out = len(words), lead, []
    for c in chunks:
        d = span * len(c) / total
        out.append({"text": " ".join(c), "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return out


def _canvas(size):
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _panel_text(img, text: str, box: tuple, kit: dict, px: float, weight: str = "bold", fill=(0, 0, 0), alpha: int = 168,
                color=(255, 255, 255), align: str = "center", max_lines: int = 2) -> tuple:
    """Draw `text` fitted into `box` (x0, y0, x1, y1; the block hugs y1 when align is "bottom", y0 otherwise) on a
    rounded panel. Returns the panel's box."""
    fam = kit.get("font") if kit.get("font") in textlayer.FONTS else textlayer.DEFAULT_FONT
    x0, y0, x1, y1 = box
    pad = px * 0.42
    fit = textlayer.fit_text(text, fam, weight, (x1 - x0) - 2 * pad, (y1 - y0) - 2 * pad, px, px * 0.6,
                             max_lines=max_lines, leading=1.18)
    font = textlayer._font(fam, weight, fit["px"])
    step = fit["px"] * fit["leading"]
    bw, bh = fit["width"] + 2 * pad, len(fit["lines"]) * step + 2 * pad * 0.8
    bx = x0 + ((x1 - x0) - bw) / 2 if align in ("center", "bottom") else (x1 - bw if align == "right" else x0)
    by = y1 - bh if align == "bottom" else y0
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=fit["px"] * 0.38, fill=(*fill[:3], alpha))
    for i, ln in enumerate(fit["lines"]):
        lw = font.getlength(ln)
        d.text((bx + (bw - lw) / 2, by + pad * 0.8 + i * step - fit["px"] * 0.08), ln, font=font, fill=(*color[:3], 255))
    return (int(bx), int(by), int(bx + bw), int(by + bh))


def caption_png(text: str, size: tuple, path: str, kit: dict | None = None, aspect: str = "", high: bool = False) -> tuple:
    """One caption chunk as a full-frame transparent PNG at `path`. Returns the panel's box (inside the safe area)."""
    kit = dict(textlayer.DEFAULT_KIT, **(kit or {}))
    W, H = size
    sx0, sy0, sx1, sy1 = textlayer.safe_box(size, aspect)
    img = _canvas(size)
    band = H * 0.16
    box = (sx0, sy0 + H * 0.07, sx1, sy0 + H * 0.07 + band) if high else (sx0, sy1 - band, sx1, sy1)
    out = _panel_text(img, text, box, kit, W * 0.062, align="center" if high else "bottom")
    img.save(path, "PNG")
    return out


def tag_png(text: str, size: tuple, path: str, kit: dict | None = None, aspect: str = "", corner: str = "left",
            accent: bool = True) -> tuple:
    """A small pill at the top of the safe area: a beat's tag (left, in the brand's accent colour) or a standing
    label such as "AI presenter" (right, white on dark). Returns its box."""
    kit = dict(textlayer.DEFAULT_KIT, **(kit or {}))
    W, H = size
    sx0, sy0, sx1, sy1 = textlayer.safe_box(size, aspect)
    img = _canvas(size)
    fill = textlayer.hex_rgb(kit.get("accent"), (245, 179, 1)) if accent else (0, 0, 0)
    color = textlayer.on_color(fill) if accent else (255, 255, 255)
    out = _panel_text(img, text, (sx0, sy0, sx1, sy0 + H * 0.07), kit, W * (0.046 if accent else 0.036),
                      fill=fill, alpha=255 if accent else 150, color=color, align=corner, max_lines=1)
    img.save(path, "PNG")
    return out


def cta_for(category: str = "") -> str:
    return CTA_DEFAULT.get(category or "", CTA_DEFAULT[""])


def end_card(size: tuple, path: str, *, packshot: str | None, kit: dict | None = None, aspect: str = "",
             headline: str = "", subline: str = "", badge: str = "", cta: str = "") -> dict:
    """The end card as a picture at `path`: the real product photo (never a generated one) set on the brand colour,
    then its words as real type by textlayer.render ("offer" layout when there is a badge, else "headline").
    Returns textlayer's report plus {"packshot": its box or None}."""
    kit = dict(textlayer.DEFAULT_KIT, **(kit or {}))
    W, H = size
    bg = textlayer.hex_rgb(kit.get("primary"), textlayer.INK)
    card = Image.new("RGB", size, bg)
    box = None
    if packshot and os.path.exists(packshot):
        try:
            import imagesafe
            imagesafe.probe(packshot, max_pixels=imagesafe.MAX_PIXELS, max_bytes=None)
            with Image.open(packshot) as im:
                shot = im.convert("RGB")
            shot.thumbnail((int(W * 0.58), int(H * 0.40)), Image.LANCZOS)
            x, y = (W - shot.size[0]) // 2, int(H * 0.60 - shot.size[1] / 2)
            mask = Image.new("L", shot.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, shot.size[0] - 1, shot.size[1] - 1),
                                                   radius=int(min(shot.size) * 0.06), fill=255)
            card.paste(shot, (x, y), mask)
            box = (x, y, x + shot.size[0], y + shot.size[1])
        except Exception:  # noqa: BLE001 — a photo that can't be read leaves a card of words, never a failed video
            box = None
    text = {"headline": " ".join(str(headline or "").split())[:80], "subline": " ".join(str(subline or "").split())[:80],
            "cta": " ".join(str(cta or "").split())[:30], "badge": " ".join(str(badge or "").split())[:28]}
    out, report = textlayer.render(card, "offer" if text["badge"] else "headline", text, kit, aspect, people=[])
    out.convert("RGB").save(path, "PNG")
    report["packshot"] = list(box) if box else None
    return report

"""The text and logo layer of a static ad (quality wave 2).

The image model is asked for a picture with no words on it; this module then sets the words as real type with
Pillow, so a headline is never misspelt, a price is never invented and the type is the brand's. Layers:

    headline, subhead     over the picture, in the top of the safe area
    badge                 a round price / offer sticker (offer preset)
    cta                   one button
    callouts              up to four feature pills beside the product
    review                a card with the customer's stars, their words and their name
    compare               a two-column card: the usual way against yours
    logo                  the brand's own logo file, when the brand kit has one

Rules the renderer keeps, and reports for every layer (render() returns the picture and a report):

  * safe areas: nothing is drawn outside SAFE[aspect], the part of the frame a platform's own buttons and
    captions leave alone (a 9:16 story keeps its top 14% and bottom 20% clear);
  * auto-fit: text is wrapped to its box and its size stepped down until it fits; text that still doesn't fit
    at the smallest size is cut with an ellipsis and reported as fit=False;
  * contrast: words on the picture are white or near-black, whichever reads better on what is under them; when
    that is still under 4.5:1 (WCAG AA) against the worst part of the box, a panel goes behind the words.
    Buttons, badges and cards have their own background, so their text colour is picked against it.

Colours and the font come from the brand kit (brands.kit_primary, kit_accent, kit_font, kit_logo; kit_of()),
with plain defaults when a brand hasn't set them. Fonts are bundled in static/fonts/ads under the SIL Open Font
License (licence files beside them; THIRD_PARTY_NOTICES.md): Poppins (sans) and DM Serif Display (serif heads).

No network, no model: everything here is local drawing.
"""
import os
import re
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "static", "fonts", "ads")
# family -> {weight: file}. "serif" sets headlines in DM Serif Display and everything small in Poppins.
FONTS = {
    "sans": {"name": "Poppins", "bold": "Poppins-Bold.ttf", "regular": "Poppins-Regular.ttf"},
    "serif": {"name": "DM Serif Display", "bold": "DMSerifDisplay-Regular.ttf", "regular": "Poppins-Regular.ttf",
              "small_bold": "Poppins-Bold.ttf"},
}
DEFAULT_FONT = "sans"
DEFAULT_KIT = {"primary": "#141414", "accent": "#F5B301", "font": DEFAULT_FONT, "logo": "", "brand": ""}

# (left, top, right, bottom) margins as parts of the width / height that stay clear, per shape.
SAFE = {"9:16": (0.07, 0.14, 0.07, 0.20), "4:5": (0.06, 0.06, 0.06, 0.07), "1:1": (0.06, 0.06, 0.06, 0.06),
        "16:9": (0.05, 0.08, 0.05, 0.08)}
_RATIOS = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "16:9": 16 / 9}
MIN_CONTRAST = 4.5
SS = 2                      # shapes and type are drawn at 2x and scaled down, so edges are smooth
WHITE, INK, BLACK = (255, 255, 255), (17, 17, 17), (0, 0, 0)
LAYOUTS = ("headline", "offer", "callouts", "review", "compare", "none")
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


class LayerFail(RuntimeError):
    """The layer couldn't be drawn (no picture to draw on, a broken file)."""


# ---------------------------------------------------------------- colour

def hex_rgb(value, default=INK) -> tuple:
    m = _HEX.match(str(value or "").strip())
    if not m:
        return default
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def clean_hex(value) -> str:
    """"#rrggbb" in lower case, or "" when it isn't a colour."""
    m = _HEX.match(str(value or "").strip())
    return "#" + m.group(1).lower() if m else ""


def luminance(rgb) -> float:
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b) -> float:
    """WCAG contrast ratio of two colours (1 to 21)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def on_color(bg) -> tuple:
    """White or black, whichever reads better on a solid background (always at least 4.5:1)."""
    return WHITE if contrast(WHITE, bg) >= contrast(BLACK, bg) else BLACK


def _blend(top, under, alpha: float) -> tuple:
    return tuple(int(round(top[i] * alpha + under[i] * (1 - alpha))) for i in range(3))


# ---------------------------------------------------------------- type

@lru_cache(maxsize=256)
def _font(family: str, weight: str, px: int):
    fam = FONTS.get(family) or FONTS[DEFAULT_FONT]
    return ImageFont.truetype(os.path.join(FONT_DIR, fam.get(weight) or fam["regular"]), max(6, int(px)))


def _width(font, text: str) -> float:
    return font.getlength(text)


def wrap(text: str, font, max_w: float) -> list:
    """Greedy word wrap; a single word wider than the box is broken by letters."""
    lines, cur = [], ""
    for word in " ".join(str(text or "").split()).split(" "):
        if not word:
            continue
        trial = (cur + " " + word).strip()
        if _width(font, trial) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        while _width(font, cur) > max_w and len(cur) > 1:
            cut = len(cur) - 1
            while cut > 1 and _width(font, cur[:cut]) > max_w:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    return lines


def fit_text(text: str, family: str, weight: str, box_w: float, box_h: float, max_px: float, min_px: float,
             max_lines: int = 3, leading: float = 1.16) -> dict:
    """The largest size from max_px down to min_px at which `text` wraps into the box in at most max_lines.
    {font, lines, px, height, width, fit}; fit is False when even min_px doesn't fit (the text is then cut)."""
    px = int(max_px)
    floor = max(6, int(min_px))
    while True:
        font = _font(family, weight, px)
        lines = wrap(text, font, box_w)
        height = len(lines) * px * leading
        if (len(lines) <= max_lines and height <= box_h) or px <= floor:
            break
        px = max(floor, int(px * 0.94) if int(px * 0.94) < px else px - 1)
    ok = len(lines) <= max_lines and height <= box_h
    if not ok:
        keep = max(1, min(max_lines, int(box_h // (px * leading)) or 1))
        lines = lines[:keep]
        last = lines[-1]
        while last and _width(font, last + "…") > box_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
        height = len(lines) * px * leading
    return {"font": font, "lines": lines, "px": px, "height": height, "leading": leading,
            "width": max([_width(font, ln) for ln in lines] or [0]), "fit": ok}


# ---------------------------------------------------------------- geometry

def aspect_of(size, aspect: str = "") -> str:
    """The named shape closest to this picture (or the one given, when it is one we know)."""
    if aspect in SAFE:
        return aspect
    r = size[0] / max(1, size[1])
    return min(_RATIOS, key=lambda k: abs(_RATIOS[k] - r))


def safe_box(size, aspect: str = "") -> tuple:
    """(x0, y0, x1, y1) in pixels: where layers may be drawn."""
    w, h = size
    left, top, right, bottom = SAFE[aspect_of(size, aspect)]
    return int(round(w * left)), int(round(h * top)), int(round(w * (1 - right))), int(round(h * (1 - bottom)))


def _inside(box, safe) -> bool:
    return box[0] >= safe[0] - 1 and box[1] >= safe[1] - 1 and box[2] <= safe[2] + 1 and box[3] <= safe[3] + 1


def region_stats(img, box) -> dict:
    """Mean colour of a region and the luminance of its darkest and lightest tenth."""
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.size[0], max(x0 + 1, x1)), min(img.size[1], max(y0 + 1, y1))
    crop = img.crop((x0, y0, x1, y1)).convert("RGB").resize((32, 32), Image.BILINEAR)
    raw = crop.tobytes()
    px = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]
    mean = tuple(sum(p[i] for p in px) // len(px) for i in range(3))
    lums = sorted(luminance(p) for p in px)
    k = max(1, len(lums) // 10)
    return {"mean": mean, "dark": sum(lums[:k]) / k, "light": sum(lums[-k:]) / k}


def _lum_contrast(color, lum: float) -> float:
    lc = luminance(color)
    hi, lo = max(lc, lum), min(lc, lum)
    return (hi + 0.05) / (lo + 0.05)


def pick_over_image(img, box) -> dict:
    """The text colour for words set straight on the picture, and the panel they need when the picture under
    them is too busy or too mid-toned: {color, scrim (None or (rgb, alpha)), contrast}."""
    st = region_stats(img, box)
    white_worst, ink_worst = _lum_contrast(WHITE, st["light"]), _lum_contrast(INK, st["dark"])
    color, worst = (WHITE, white_worst) if white_worst >= ink_worst else (INK, ink_worst)
    if worst >= MIN_CONTRAST:
        return {"color": color, "scrim": None, "contrast": round(worst, 2)}
    panel = BLACK if color == WHITE else WHITE
    for alpha in (0.62, 0.74, 0.86, 0.94):
        under = _blend(panel, WHITE if color == WHITE else BLACK, alpha)   # the worst pixel the panel can sit on
        c = contrast(color, under)
        if c >= MIN_CONTRAST:
            return {"color": color, "scrim": (panel, alpha), "contrast": round(c, 2)}
    return {"color": color, "scrim": (panel, 1.0), "contrast": round(contrast(color, panel), 2)}


# ---------------------------------------------------------------- drawing

class _Canvas:
    """A transparent layer at SS x the picture; coordinates are given in picture pixels."""

    def __init__(self, size):
        self.size = size
        self.im = Image.new("RGBA", (size[0] * SS, size[1] * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def _s(self, box):
        return [int(round(v * SS)) for v in box]

    def rect(self, box, fill, radius: float = 0, alpha: float = 1.0):
        self.d.rounded_rectangle(self._s(box), radius=int(radius * SS), fill=(*fill[:3], int(round(255 * alpha))))

    def ellipse(self, box, fill):
        self.d.ellipse(self._s(box), fill=(*fill[:3], 255))

    def line(self, pts, fill, width: float):
        self.d.line([(int(x * SS), int(y * SS)) for x, y in pts], fill=(*fill[:3], 255), width=max(1, int(width * SS)),
                    joint="curve")

    def polygon(self, pts, fill):
        self.d.polygon([(int(x * SS), int(y * SS)) for x, y in pts], fill=(*fill[:3], 255))

    def text_block(self, fitted: dict, x: float, y: float, w: float, color, family: str, weight: str,
                   align: str = "center"):
        """Draw fitted lines in a box starting at (x, y), w wide; returns the block's (x0, y0, x1, y1)."""
        font = _font(family, weight, fitted["px"] * SS)
        step = fitted["px"] * fitted["leading"]
        x0, x1 = x + w, x
        for i, ln in enumerate(fitted["lines"]):
            lw = font.getlength(ln) / SS
            lx = x + (w - lw) / 2 if align == "center" else x + w - lw if align == "right" else x
            self.d.text((int(lx * SS), int((y + i * step) * SS)), ln, font=font, fill=(*color[:3], 255))
            x0, x1 = min(x0, lx), max(x1, lx + lw)
        return (x0, y, x1, y + len(fitted["lines"]) * step)

    def onto(self, base):
        layer = self.im.resize(self.size, Image.LANCZOS)
        return Image.alpha_composite(base.convert("RGBA"), layer)


def _star(cv, cx, cy, r, fill):
    import math
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    cv.polygon(pts, fill)


def kit_of(brand_row=None) -> dict:
    """The brand kit a layer is drawn with, from a brands row (any missing part falls back to DEFAULT_KIT)."""
    kit = dict(DEFAULT_KIT)
    if brand_row is None:
        return kit
    keys = brand_row.keys() if hasattr(brand_row, "keys") else []
    get = (lambda k: (brand_row[k] if k in keys else None) or "")
    kit["primary"] = clean_hex(get("kit_primary")) or kit["primary"]
    kit["accent"] = clean_hex(get("kit_accent")) or kit["accent"]
    kit["font"] = get("kit_font") if get("kit_font") in FONTS else kit["font"]
    kit["logo"] = get("kit_logo") if get("kit_logo") and os.path.exists(get("kit_logo")) else ""
    kit["brand"] = get("brand_name")
    return kit


def _logo(path: str, max_w: int, max_h: int):
    try:
        import imagesafe
        imagesafe.probe(path, max_pixels=imagesafe.LOGO_MAX_PIXELS, max_bytes=None)
        with Image.open(path) as im:
            im = im.convert("RGBA")
            im.thumbnail((max_w, max_h), Image.LANCZOS)
            return im.copy()
    except Exception:  # noqa: BLE001 — a broken logo file never stops an ad; it is just left off
        return None


# ---------------------------------------------------------------- the layouts

def render(img, layout: str, text: dict, kit: dict | None = None, aspect: str = ""):
    """(picture with its layers, report). `img` is the text-free picture; `text` holds headline, subline, cta,
    badge, callouts [..], review {quote, name, stars} and compare {left_title, right_title, left [..], right [..]};
    only what the layout uses is drawn. The report: {"aspect", "safe", "ok", "layers": {name: {...}}}."""
    kit = dict(DEFAULT_KIT, **(kit or {}))
    text = text or {}
    base = img.convert("RGB")
    W, H = base.size
    asp = aspect_of(base.size, aspect)
    safe = safe_box(base.size, asp)
    sx0, sy0, sx1, sy1 = safe
    sw, sh = sx1 - sx0, sy1 - sy0
    report = {"aspect": asp, "safe": list(safe), "layers": {}, "ok": True}
    if layout not in LAYOUTS or layout == "none":
        return base, report
    fam = kit["font"] if kit.get("font") in FONTS else DEFAULT_FONT
    small_bold = "small_bold" if FONTS[fam].get("small_bold") else "bold"
    primary, accent = hex_rgb(kit.get("primary"), INK), hex_rgb(kit.get("accent"), (245, 179, 1))
    cv = _Canvas(base.size)
    unit = min(W, H)

    def note(name, box, **kw):
        box = [int(round(v)) for v in box]
        kw["inside"] = _inside(box, safe)
        kw["box"] = box
        report["layers"][name] = kw
        if not kw["inside"] or kw.get("fit") is False or kw.get("contrast", 99) < MIN_CONTRAST:
            report["ok"] = False

    def clean(v, lim=400):
        return " ".join(str(v or "").split())[:lim]

    headline, subline, cta = clean(text.get("headline"), 160), clean(text.get("subline"), 160), clean(text.get("cta"), 40)
    pad = unit * 0.022
    top = sy0

    # ---- headline and subhead: straight on the picture, at the top of the safe area
    if headline and layout in ("headline", "offer", "callouts", "compare"):
        box_h = sh * (0.20 if layout == "compare" else 0.26)
        fh = fit_text(headline, fam, "bold", sw - 2 * pad, box_h, W * (0.064 if layout == "compare" else 0.082),
                      W * 0.040, max_lines=3, leading=1.14)
        fs = None
        if subline and layout in ("headline", "offer"):
            fs = fit_text(subline, fam, "regular", sw - 2 * pad, sh * 0.10, W * 0.038, W * 0.028, max_lines=2,
                          leading=1.25)
        block_h = fh["height"] + (fs["height"] + pad * 0.6 if fs else 0)
        block_w = max(fh["width"], fs["width"] if fs else 0)
        bx0 = sx0 + (sw - block_w) / 2 - pad
        panel = (max(sx0, bx0), sy0, min(sx1, bx0 + block_w + 2 * pad), sy0 + block_h + 2 * pad)
        pick = pick_over_image(base, panel)
        if pick["scrim"]:
            cv.rect(panel, pick["scrim"][0], radius=unit * 0.02, alpha=pick["scrim"][1])
        hb = cv.text_block(fh, sx0 + pad, sy0 + pad, sw - 2 * pad, pick["color"], fam, "bold")
        note("headline", (max(hb[0], sx0), hb[1], min(hb[2], sx1), hb[3]), px=fh["px"], lines=len(fh["lines"]),
             fit=fh["fit"], contrast=pick["contrast"], scrim=bool(pick["scrim"]), color=list(pick["color"]))
        top = hb[3]
        if fs:
            sb = cv.text_block(fs, sx0 + pad, hb[3] + pad * 0.6, sw - 2 * pad, pick["color"], fam, "regular")
            note("subhead", (max(sb[0], sx0), sb[1], min(sb[2], sx1), sb[3]), px=fs["px"], lines=len(fs["lines"]),
                 fit=fs["fit"], contrast=pick["contrast"], scrim=bool(pick["scrim"]))
            top = sb[3]
        top += pad

    # ---- one button, bottom centre
    bottom = sy1
    if cta and layout in ("headline", "offer", "callouts"):
        fc = fit_text(cta, fam, small_bold, sw * 0.62, unit * 0.06, W * 0.040, W * 0.028, max_lines=1)
        bw, bh = fc["width"] + unit * 0.09, fc["px"] * 2.2
        bx, by = sx0 + (sw - bw) / 2, sy1 - bh
        cv.rect((bx, by, bx + bw, by + bh), primary, radius=bh / 2)
        col = on_color(primary)
        cv.text_block(fc, bx, by + (bh - fc["px"] * fc["leading"]) / 2, bw, col, fam, small_bold)
        note("cta", (bx, by, bx + bw, by + bh), px=fc["px"], fit=fc["fit"], contrast=round(contrast(col, primary), 2))
        bottom = by - pad

    # ---- offer: a round badge to the right of the product
    badge = clean(text.get("badge"), 40)
    if badge and layout == "offer":
        d = unit * 0.27
        cx, cy = sx1 - d / 2, max(top + d / 2, sy0 + sh * 0.40)
        cy = min(cy, bottom - d / 2)
        cv.ellipse((cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2), accent)
        col = on_color(accent)
        inner = d * 0.74
        fb = fit_text(badge, fam, small_bold, inner, inner * 0.74, d * 0.30, d * 0.11, max_lines=3, leading=1.08)
        cv.text_block(fb, cx - inner / 2, cy - fb["height"] / 2, inner, col, fam, small_bold)
        note("badge", (cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2), px=fb["px"], lines=len(fb["lines"]),
             fit=fb["fit"], contrast=round(contrast(col, accent), 2))

    # ---- feature callouts: pills down the left side
    callouts = [clean(c, 60) for c in (text.get("callouts") or []) if clean(c)][:4]
    if callouts and layout == "callouts":
        pw = sw * 0.50
        y = max(top + pad, sy0 + sh * 0.30)
        room = (bottom - y) / len(callouts)
        fits = [fit_text(c, fam, "regular", pw - unit * 0.10, min(room - pad, unit * 0.12), W * 0.034, W * 0.024,
                         max_lines=2, leading=1.18) for c in callouts]
        px = min(f["px"] for f in fits)
        fits = [fit_text(c, fam, "regular", pw - unit * 0.10, min(room - pad, unit * 0.12), px, px, max_lines=2,
                         leading=1.18) for c in callouts]
        for i, f in enumerate(fits):
            ph = f["height"] + unit * 0.034
            box = (sx0, y, sx0 + min(pw, f["width"] + unit * 0.10), y + ph)
            cv.rect(box, WHITE, radius=min(ph / 2, unit * 0.03))
            dot = unit * 0.012
            cv.ellipse((sx0 + unit * 0.028 - dot, y + ph / 2 - dot, sx0 + unit * 0.028 + dot, y + ph / 2 + dot), accent
                       if contrast(accent, WHITE) >= 1.6 else primary)
            cv.text_block(f, sx0 + unit * 0.058, y + unit * 0.017, pw - unit * 0.10, INK, fam, "regular", align="left")
            note(f"callout{i + 1}", box, px=f["px"], lines=len(f["lines"]), fit=f["fit"],
                 contrast=round(contrast(INK, WHITE), 2))
            y += ph + pad * 0.8

    # ---- review card: stars, the customer's words, their name
    review = text.get("review") or {}
    quote = clean(review.get("quote"), 260)
    if quote and layout == "review":
        inner_w = sw - unit * 0.10
        max_h = sh * 0.40
        stars = int(review.get("stars") or 0) if str(review.get("stars") or "0").isdigit() else 0
        name = clean(review.get("name"), 48)
        star_r = unit * 0.022
        head_h = (star_r * 2 + pad) if 1 <= stars <= 5 else 0
        fn = fit_text("— " + name, fam, "regular", inner_w, unit * 0.05, W * 0.028, W * 0.022, max_lines=1) if name else None
        foot_h = (fn["height"] + pad * 0.6) if fn else 0
        fq = fit_text("“" + quote + "”", fam, "regular", inner_w, max_h - head_h - foot_h - unit * 0.07,
                      W * 0.040, W * 0.024, max_lines=7, leading=1.28)
        card_h = head_h + fq["height"] + foot_h + unit * 0.07
        box = (sx0, sy1 - card_h, sx1, sy1)
        cv.rect(box, WHITE, radius=unit * 0.03)
        y = box[1] + unit * 0.035
        if head_h:
            star_fill = accent if contrast(accent, WHITE) >= 1.3 else (245, 179, 1)
            for i in range(5):
                _star(cv, sx0 + unit * 0.05 + star_r + i * star_r * 2.5, y + star_r, star_r,
                      star_fill if i < stars else (214, 214, 214))
            y += head_h
        cv.text_block(fq, sx0 + unit * 0.05, y, inner_w, INK, fam, "regular", align="left")
        y += fq["height"] + pad * 0.6
        if fn:
            cv.text_block(fn, sx0 + unit * 0.05, y, inner_w, (85, 85, 85), fam, "regular", align="left")
        note("review", box, px=fq["px"], lines=len(fq["lines"]), fit=fq["fit"] and (fn is None or fn["fit"]),
             contrast=round(min(contrast(INK, WHITE), contrast((85, 85, 85), WHITE)), 2), stars=stars)

    # ---- comparison card: two columns, a cross and a tick per row
    cmp_ = text.get("compare") or {}
    left = [clean(x, 60) for x in (cmp_.get("left") or []) if clean(x)][:3]
    right = [clean(x, 60) for x in (cmp_.get("right") or []) if clean(x)][:3]
    rows = min(len(left), len(right))
    if rows and layout == "compare":
        gap = unit * 0.012
        col_w = (sw - gap) / 2
        head_h = unit * 0.075
        row_h = min(unit * 0.105, (sh * 0.44 - head_h) / rows)
        card_h = head_h + rows * row_h
        y0 = sy1 - card_h
        grey = (236, 236, 236)
        cells = [(left, clean(cmp_.get("left_title"), 30) or "The usual way", grey, sx0),
                 (right, clean(cmp_.get("right_title"), 30) or "Yours", primary, sx0 + col_w + gap)]
        texts_w = col_w - unit * 0.115
        fits = [fit_text(t, fam, "regular", texts_w, row_h - unit * 0.02, W * 0.030, W * 0.021, max_lines=2, leading=1.15)
                for col in (left[:rows], right[:rows]) for t in col]
        px = min(f["px"] for f in fits)
        all_fit, worst = True, 99.0
        for items, title, head, x in cells:
            cv.rect((x, y0, x + col_w, y0 + card_h), WHITE, radius=unit * 0.022)
            cv.rect((x, y0, x + col_w, y0 + head_h), head, radius=unit * 0.022)
            cv.rect((x, y0 + head_h / 2, x + col_w, y0 + head_h), head)
            hc = on_color(head)
            ft = fit_text(title, fam, small_bold, col_w - unit * 0.05, head_h * 0.8, W * 0.030, W * 0.021, max_lines=1)
            cv.text_block(ft, x, y0 + (head_h - ft["px"] * ft["leading"]) / 2, col_w, hc, fam, small_bold)
            all_fit = all_fit and ft["fit"]
            worst = min(worst, contrast(hc, head))
            yours = head == primary
            for i, t in enumerate(items[:rows]):
                ry = y0 + head_h + i * row_h
                f = fit_text(t, fam, "regular", texts_w, row_h - unit * 0.02, px, px, max_lines=2, leading=1.15)
                all_fit = all_fit and f["fit"]
                mx, my, r = x + unit * 0.045, ry + row_h / 2, unit * 0.016
                if yours:
                    mark = primary if contrast(primary, WHITE) >= 3 else (22, 128, 60)
                    cv.line([(mx - r, my), (mx - r * 0.3, my + r * 0.7), (mx + r, my - r * 0.7)], mark, unit * 0.008)
                else:
                    cv.line([(mx - r * 0.8, my - r * 0.8), (mx + r * 0.8, my + r * 0.8)], (150, 150, 150), unit * 0.007)
                    cv.line([(mx - r * 0.8, my + r * 0.8), (mx + r * 0.8, my - r * 0.8)], (150, 150, 150), unit * 0.007)
                cv.text_block(f, x + unit * 0.085, ry + (row_h - f["height"]) / 2, texts_w, INK, fam, "regular",
                              align="left")
                if i:
                    cv.line([(x + unit * 0.02, ry), (x + col_w - unit * 0.02, ry)], (228, 228, 228), 1)
        worst = min(worst, contrast(INK, WHITE))
        note("compare", (sx0, y0, sx1, sy1), px=px, rows=rows, fit=all_fit, contrast=round(worst, 2))

    out = cv.onto(base)

    # ---- the brand's own logo file (never redrawn): a corner of the safe area that nothing else uses
    if kit.get("logo"):
        lg = _logo(kit["logo"], int(sw * 0.22), int(sh * 0.08))
        if lg is not None:
            at_top = layout == "review"
            lx = sx0
            ly = sy0 if at_top else sy1 - lg.size[1]
            if layout == "compare":
                lx, ly = sx1 - lg.size[0], max(sy0, int(top))
            box = (lx, ly, lx + lg.size[0], ly + lg.size[1])
            clash = any(_overlap(box, v["box"]) for v in report["layers"].values())
            if not clash:
                out.alpha_composite(lg, (int(lx), int(ly)))
                note("logo", box, fit=True)
    return out.convert("RGB"), report


def _overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


# ---------------------------------------------------------------- files

def base_path(path: str) -> str:
    """Where the text-free picture is kept beside the finished one (a free fix or a re-edit starts from it)."""
    stem, ext = os.path.splitext(path)
    return stem + ".base" + ext


def text_from_params(p: dict) -> dict:
    """The words of a job (its params) in render()'s shape."""
    lay = p.get("layers") if isinstance(p.get("layers"), dict) else {}
    layout = p.get("layout") or ""
    out = {"headline": "" if layout == "review" else p.get("headline") or "", "subline": p.get("subline") or "",
           "cta": p.get("cta") or "", "badge": lay.get("badge") or "", "callouts": lay.get("callouts") or []}
    if layout == "review":
        out["review"] = {"quote": p.get("headline") or "", "name": lay.get("reviewer") or "", "stars": lay.get("stars") or 0}
    if layout == "compare":
        out["compare"] = {"left_title": lay.get("left_title") or "", "right_title": lay.get("right_title") or "",
                          "left": lay.get("left") or [], "right": lay.get("right") or []}
    return out


def apply_to_file(path: str, layout: str, text: dict, kit: dict | None = None, aspect: str = "") -> dict:
    """Set the layers on a saved picture, in place; the text-free original is kept at base_path(path).
    Returns the report. Raises LayerFail when there is no readable picture."""
    if not path or not os.path.exists(path):
        raise LayerFail("no picture to set the text on")
    try:
        import imagesafe
        imagesafe.probe(path, max_pixels=imagesafe.MAX_PIXELS, max_bytes=None)
        with Image.open(path) as im:
            img = im.convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise LayerFail("the picture couldn't be read") from e
    out, report = render(img, layout, text, kit, aspect)
    if layout == "none" or layout not in LAYOUTS:
        return report
    keep = base_path(path)
    if not os.path.exists(keep):
        img.save(keep, **_save_args(keep))
    tmp = path + ".tmp"
    out.save(tmp, **_save_args(path))
    os.replace(tmp, path)
    return report


def _save_args(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return {"format": "JPEG", "quality": 93}
    if ext == ".webp":
        return {"format": "WEBP", "quality": 93}
    return {"format": "PNG"}

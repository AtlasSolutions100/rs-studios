"""Placeholder thumbnails for the newer presets in Create (static/previews/preset-<id>.jpg).

They are drawings, not generated ads: a plain backdrop, a stand-in product shape, and (for the statics with a
text layer) the real layer drawn by textlayer.py with sample words, so the thumbnail shows where the words go.
Swap any of them for a real example by saving a 480 px wide JPEG under the same name.

    python tools/preset_thumbs.py            # writes every missing thumbnail
    python tools/preset_thumbs.py --all      # rewrites all of them

No network, no model.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

import presets  # noqa: E402
import textlayer  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "previews")
SIZES = {"9:16": (720, 1280), "4:5": (800, 1000), "1:1": (900, 900), "16:9": (1280, 720)}
# backdrop (top colour, bottom colour) per preset; the stand-in product sits where the layout leaves room
TONES = {"shelf": ((222, 214, 200), (188, 172, 150)), "closeup": ((206, 214, 222), (160, 172, 186)),
         "doorstep": ((214, 196, 170), (170, 140, 104)), "table": ((226, 200, 170), (176, 130, 92)),
         "counter": ((236, 232, 222), (204, 196, 180)), "vanity": ((220, 232, 232), (176, 200, 200)),
         "pastel": ((246, 222, 226), (236, 198, 206)), "outfit": ((232, 228, 220), (200, 192, 180)),
         "street": ((190, 198, 206), (120, 130, 142)), "nightstand": ((92, 74, 60), (52, 40, 34)),
         "cozy": ((214, 180, 140), (150, 110, 78)), "windowsill": ((244, 236, 210), (214, 198, 150)),
         "desk": ((220, 224, 230), (176, 184, 196)), "inuse": ((206, 190, 170), (140, 120, 100)),
         "clean-headline": ((232, 226, 216), (196, 184, 168)), "offer": ((250, 232, 200), (240, 206, 150)),
         "callouts": ((226, 234, 240), (190, 204, 216)), "review": ((238, 230, 222), (208, 194, 180)),
         "compare": ((228, 232, 226), (196, 204, 192)), "marketplace": ((255, 255, 255), (255, 255, 255))}
SAMPLE = {"clean-headline": {"headline": "Your headline, in real type", "subline": "Set after the picture is made",
                             "cta": "Shop now"},
          "offer": {"headline": "The everyday one", "badge": "$24", "cta": "Get yours"},
          "callouts": {"headline": "What's inside", "callouts": ["Feature one", "Feature two", "Feature three"],
                       "cta": "See more"},
          "review": {"review": {"quote": "A real review you paste goes here, word for word.", "name": "A customer",
                                "stars": 5}},
          "compare": {"headline": "Why switch", "compare": {"left_title": "The usual way", "right_title": "Yours",
                                                            "left": ["Slow", "Fiddly", "Costs more"],
                                                            "right": ["Quick", "Simple", "Costs less"]}}}
PLACE = {"callouts": (0.70, 0.60), "review": (0.50, 0.34), "compare": (0.50, 0.33), "offer": (0.44, 0.60),
         "headline": (0.50, 0.64), "none": (0.50, 0.52)}


def scene(size, tones, at=(0.5, 0.6), scale=0.34):
    w, h = size
    im = Image.new("RGB", size, tones[0])
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(tones[0][i] + (tones[1][i] - tones[0][i]) * t) for i in range(3)))
    pw, ph = w * scale * 0.62, h * scale
    cx, cy = w * at[0], h * at[1]
    if tones[0] != (255, 255, 255):
        sh = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse((cx - pw * 0.8, cy + ph * 0.44, cx + pw * 0.8, cy + ph * 0.56), fill=(0, 0, 0, 70))
        im = Image.alpha_composite(im.convert("RGBA"), sh.filter(ImageFilter.GaussianBlur(w * 0.012))).convert("RGB")
        d = ImageDraw.Draw(im)
    else:
        d.ellipse((cx - pw * 0.7, cy + ph * 0.47, cx + pw * 0.7, cy + ph * 0.53), fill=(232, 232, 232))
    body = (58, 78, 96)
    d.rounded_rectangle((cx - pw / 2, cy - ph * 0.36, cx + pw / 2, cy + ph / 2), radius=int(pw * 0.16), fill=body)
    d.rounded_rectangle((cx - pw * 0.2, cy - ph / 2, cx + pw * 0.2, cy - ph * 0.34), radius=int(pw * 0.05),
                        fill=(36, 48, 60))
    d.rounded_rectangle((cx - pw * 0.36, cy - ph * 0.12, cx + pw * 0.36, cy + ph * 0.26), radius=int(pw * 0.05),
                        fill=(240, 236, 226))
    return im


def make(p) -> Image.Image:
    size = SIZES.get(p.aspect or "4:5", SIZES["4:5"])
    lay = p.layout or "scene"
    im = scene(size, TONES.get(p.id, ((228, 226, 220), (196, 190, 180))), PLACE.get(lay, (0.5, 0.58)))
    if p.layout and p.layout != "none":
        im, _ = textlayer.render(im, p.layout, SAMPLE.get(p.id, {}), None, p.aspect)
    else:
        d = ImageDraw.Draw(im)
        if p.id != "marketplace":
            f = textlayer._font("sans", "bold", int(size[0] * 0.05))
            label = "Example"
            d.text(((size[0] - f.getlength(label)) / 2, size[1] * 0.08), label, font=f, fill=(255, 255, 255))
    im.thumbnail((480, 640), Image.LANCZOS)
    return im


def main(force: bool = False) -> list:
    made = []
    for p in presets.SCENES + presets.LAYERED:
        out = os.path.join(OUT, p.preview + ".jpg")
        if force or not os.path.exists(out):
            make(p).save(out, "JPEG", quality=78, optimize=True)
            made.append(out)
    return made


if __name__ == "__main__":
    for path in main("--all" in sys.argv):
        print("wrote", os.path.relpath(path))

"""The text layer keeps its words off the product (textlayer.py: locate_product, the moves, recompose, wrap).

The image model is asked to leave the text's part of the frame calm and often doesn't, so the layer finds the
product itself and makes room. Checked here on drawn pictures (a coloured shape on a plain backdrop), with the
shape put exactly where each layout's words would land:

  * the locator finds the shape's box, ignores its soft shadow, and says None for a blank picture or a scene
  * after render no layer's box touches the product, the whole product is still in the picture (never cropped,
    never drawn over), every layer is inside the safe area and fitted, and contrast is at least 4.5:1
  * a part that can move is moved (button, badge, pills, review card) and the picture is left alone; when no
    move clears it the picture is re-composed, scaled down on its own extended backdrop, and the report says so
  * a picture where the product can't be told is rendered exactly as before
  * wrapping: no line ends or starts with a separator, and a price keeps its neighbour

No network, no model, no database: Pillow only.

    python tests/test_textlayer_fit.py      (tests/test_app.py runs it too)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PIL import Image, ImageChops, ImageDraw, ImageFilter  # noqa: E402

import textlayer  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


BLOB = (196, 28, 64)
KIT = {"primary": "#1F4D3A", "accent": "#F2B705"}
WORDS = {"headline": "Hot coffee from sunrise to summit", "subline": "Keeps drinks hot for 12 hours", "cta": "Shop now",
         "badge": "$29 · Free shipping", "callouts": ["Hot for 12 hours", "Leak-proof flip lid", "Fits cup holders",
                                                     "Dishwasher safe"],
         "review": {"quote": "Took it up three mountains this summer and my coffee was still too hot to gulp at the top.",
                    "name": "Dana R.", "stars": 5},
         "compare": {"left_title": "The usual way", "right_title": "Northpine",
                     "left": ["Lukewarm by 10am", "Leaks in your bag", "Won't fit the car"],
                     "right": ["Hot for 12 hours", "Locks shut", "Fits cup holders"]}}
SIZES = {"4:5": (800, 1000), "1:1": (900, 900), "9:16": (720, 1280), "16:9": (1280, 720)}
# Where the shape goes, as parts of the frame (x0, y0, x1, y1): on each spot a layout puts words.
SPOTS = {"bottom centre (the button)": (0.34, 0.50, 0.66, 0.94), "right middle (the badge)": (0.62, 0.30, 0.94, 0.80),
         "left middle (the pills)": (0.08, 0.30, 0.44, 0.84), "low and wide (the card, the table)": (0.20, 0.52, 0.80, 0.93),
         "top (the headline)": (0.30, 0.08, 0.70, 0.62), "most of the frame": (0.18, 0.16, 0.82, 0.90)}


def picture(size, spot, tone=(232, 226, 214), shadow=False):
    """A plain backdrop with a rounded shape of one colour at `spot`; returns (picture, the shape's box)."""
    w, h = size
    im = Image.new("RGB", size, tone)
    box = (int(spot[0] * w), int(spot[1] * h), int(spot[2] * w), int(spot[3] * h))
    if shadow:                                   # a soft shadow on the floor, off to one side
        sh = Image.new("L", size, 0)
        ImageDraw.Draw(sh).ellipse((box[0] - int(0.12 * w), box[3] - int(0.03 * h), box[2], box[3] + int(0.02 * h)), fill=70)
        im = Image.composite(Image.new("RGB", size, tuple(max(0, c - 60) for c in tone)), im,
                             sh.filter(ImageFilter.GaussianBlur(w * 0.02)))
    ImageDraw.Draw(im).rounded_rectangle(box, radius=min(w, h) // 20, fill=BLOB)
    return im, box


def blob_mask(im):
    """Where the picture is the shape's colour (white), as an L image."""
    r, g, b = ImageChops.difference(im.convert("RGB"), Image.new("RGB", im.size, BLOB)).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v < 40 else 0)


def area(mask) -> int:
    return mask.histogram()[255]


def overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


# ------------------------------------------------------------------ 1. the locator
print("== the product locator")
worst = 0
for asp, size in SIZES.items():
    for spot in SPOTS.values():
        for tone in ((232, 226, 214), (250, 250, 250), (30, 32, 36)):
            im, box = picture(size, spot, tone)
            got = textlayer.locate_product(im)
            if got is None:
                check(False, f"the shape at {spot} on {tone} ({asp}) is found")
            worst = max(worst, max(abs(got[i] - box[i]) for i in range(4)))
check(worst <= max(max(s) for s in SIZES.values()) * 0.012 + 2,
      f"a shape on a plain backdrop is found in every shape and tone, to within {worst}px of its real box")
im, box = picture((800, 1000), SPOTS["right middle (the badge)"], shadow=True)
got = textlayer.locate_product(im)
check(got is not None and abs(got[0] - box[0]) <= 12 and abs(got[3] - box[3]) <= 30,
      "its soft shadow is not counted as product (the box stops at the shape)")
grad = Image.linear_gradient("L").resize((800, 1000)).point(lambda v: 205 + v // 6).convert("RGB")
ImageDraw.Draw(grad).rounded_rectangle((300, 400, 520, 800), radius=30, fill=BLOB)
got = textlayer.locate_product(grad)
check(got is not None and max(abs(got[i] - (300, 400, 520, 800)[i]) for i in range(4)) <= 12,
      "a backdrop that fades from light to darker is still a backdrop")
scene = Image.new("RGB", (800, 1000), (250, 250, 250))
scene.paste((8, 8, 8), (0, 0, 400, 1000))
noisy = Image.effect_noise((800, 1000), 90).convert("RGB")
full, _ = picture((800, 1000), (0.02, 0.02, 0.98, 0.98))
check(textlayer.locate_product(Image.new("RGB", (800, 1000), (240, 240, 240))) is None
      and textlayer.locate_product(scene) is None and textlayer.locate_product(noisy) is None
      and textlayer.locate_product(full) is None and textlayer.locate_product(Image.new("RGB", (40, 40))) is None,
      "a blank picture, a scene, noise, a subject that fills the frame and a tiny picture all give None")

# ------------------------------------------------------------------ 2. no layer on the product
print("== the layers keep off the product")
done, moved, recomposed = 0, 0, 0
for asp, size in SIZES.items():
    for where, spot in SPOTS.items():
        base, box = picture(size, spot)
        before = area(blob_mask(base))
        for layout in ("headline", "offer", "callouts", "review", "compare"):
            out, rep = textlayer.render(base, layout, WORDS, KIT, asp)
            tag = f"{layout} {asp}, product {where}"
            comp = rep["compose"]
            if comp["product"] is None or not comp["clear"] or not rep["ok"]:
                check(False, f"{tag}: the product is found and the words are kept off it ({comp})")
            bad = [(n, v) for n, v in rep["layers"].items()
                   if not v["inside"] or v.get("fit") is False or v.get("contrast", 99) < textlayer.MIN_CONTRAST]
            if bad or out.size != size:
                check(False, f"{tag}: every layer inside the safe area, fitted, contrast >= 4.5 ({bad})")
            mask = blob_mask(out)
            seen = mask.getbbox()                       # where the product really is in the finished picture
            scale = comp["scale"]
            if seen is None or [n for n, v in rep["layers"].items() if overlaps(v["box"], seen)]:
                check(False, f"{tag}: no layer's box touches the product ({seen}, {rep['layers']}, {comp})")
            if seen[0] <= 0 or seen[1] <= 0 or seen[2] >= size[0] or seen[3] >= size[1]:
                check(False, f"{tag}: the product is not cut by the frame ({seen})")
            want_w, want_h = (box[2] - box[0]) * scale, (box[3] - box[1]) * scale
            if (abs((seen[2] - seen[0]) - want_w) > 4 or abs((seen[3] - seen[1]) - want_h) > 4
                    or area(mask) < before * scale * scale * 0.96):
                check(False, f"{tag}: the whole product is still there, uncropped and not drawn over "
                             f"({seen}, {area(mask)} of {before} at x{scale})")
            if comp["action"] == "recomposed":
                now = comp["product_now"]
                if scale < textlayer.MIN_SCALE or scale > 1 or max(abs(now[i] - seen[i]) for i in range(4)) > 16:
                    check(False, f"{tag}: the report says where the product went ({now} against {seen})")
                recomposed += 1
            elif comp["action"] == "moved":
                if not comp["place"] or scale != 1.0 or max(abs(seen[i] - box[i]) for i in range(4)) > 2:
                    check(False, f"{tag}: a move leaves the picture alone ({comp})")
                moved += 1
            elif comp["place"] or scale != 1.0:
                check(False, f"{tag}: nothing in the way means nothing changed ({comp})")
            done += 1
check(done == 120 and moved >= 15 and recomposed >= 15,
      f"{done} renders (5 layouts, 4 shapes, the product on 6 spots): no layer on the product, the product whole, "
      f"layers safe, fitted and at 4.5:1; {moved} by moving a part, {recomposed} by re-composing the picture")

base, box = picture((800, 1000), (0.41, 0.55, 0.59, 0.94))
_, rep = textlayer.render(base, "headline", WORDS, KIT, "4:5")
check(rep["compose"]["action"] == "moved" and rep["compose"]["place"].get("cta") in ("left", "right")
      and not overlaps(rep["layers"]["cta"]["box"], box),
      "the button under the product moves to a free side of the safe area")
base, box = picture((800, 1000), SPOTS["right middle (the badge)"])
_, rep = textlayer.render(base, "offer", WORDS, KIT, "4:5")
check(rep["compose"]["action"] == "moved" and rep["compose"]["place"].get("badge", ("right",))[0] == "left"
      and not overlaps(rep["layers"]["badge"]["box"], box), "the badge moves to the free side")
base, box = picture((800, 1000), SPOTS["left middle (the pills)"])
_, rep = textlayer.render(base, "callouts", WORDS, KIT, "4:5")
check(rep["compose"]["place"].get("callouts") == "right" and rep["layers"]["callout1"]["box"][2] == rep["safe"][2],
      "the feature pills go down the right when the product is on the left")
base, box = picture((800, 1000), (0.25, 0.50, 0.75, 0.93))
_, rep = textlayer.render(base, "review", WORDS, KIT, "4:5")
check(rep["compose"] == {"product": rep["compose"]["product"], "action": "moved", "place": {"review": "top"},
                         "scale": 1.0, "clear": True} and rep["layers"]["review"]["box"][1] == rep["safe"][1],
      "the review card goes to the top when the product stands where it would sit")
out, rep = textlayer.render(base, "compare", WORDS, KIT, "4:5")
comp = rep["compose"]
check(comp["action"] == "recomposed" and comp["scale"] < 1 and comp["product_now"][3] <= rep["layers"]["compare"]["box"][1]
      and comp["product_now"][1] >= rep["layers"]["headline"]["box"][3]
      and all(abs(out.getpixel(p)[i] - (232, 226, 214)[i]) <= 3 for p in ((3, 3), (796, 3), (3, 996), (796, 996), (30, 500))
              for i in range(3)),
      "the comparison table can't move, so the picture is re-composed: the product smaller, between the headline "
      "and the table, on more of the same backdrop")
plain = Image.new("RGB", (800, 1000), (240, 240, 240))
a, ra = textlayer.render(plain, "offer", WORDS, KIT, "4:5")
b, rb = textlayer.render(plain, "offer", WORDS, KIT, "4:5", fit=False)
c, rc = textlayer.render(scene, "review", WORDS, KIT, "4:5")
d, _ = textlayer.render(scene, "review", WORDS, KIT, "4:5", fit=False)
check(a.tobytes() == b.tobytes() and ra["layers"] == rb["layers"] and ra["compose"]["product"] is None
      and ra["compose"]["action"] == "none" and c.tobytes() == d.tobytes() and rc["compose"]["product"] is None,
      "a picture whose product can't be told (blank, or a scene) is rendered exactly as before")
small = textlayer.recompose(base, 0.5, (200, 250))
m = blob_mask(small).getbbox()
check(small.size == base.size and abs(m[0] - (200 + 100)) <= 2 and abs(m[3] - (250 + 465)) <= 2
      and textlayer.recompose(base, 1.0, (0, 0)).tobytes() == base.tobytes(),
      "recompose scales the picture onto a canvas of the same size and fills the rest from its edges")

# ------------------------------------------------------------------ 3. wrapping
print("== wrapping")
font = textlayer._font("sans", "bold", 40)
seps = tuple(textlayer.SEPARATORS)
widths = [60, 90, 120, 160, 200, 260, 330, 420, 520, 700, 2000]
texts = ["$29 · Free shipping", "$29 | Free shipping | 30 day returns", "Mug + lid - free today", "Hot / cold · all day",
         "Save 20% · this week only", "A · B · C · D · E", "· $29 ·", "Only $29 today"]
bad = [(t, w, ls) for t in texts for w in widths for ls in [textlayer.wrap(t, font, w)]
       if any(ln.split(" ")[0] in seps or ln.split(" ")[-1] in seps for ln in ls)]
check(not bad, f"no line ends or starts with a separator, at any width ({bad[:2]})")
w_all = font.getlength("$29 · Free shipping")
check(textlayer.wrap("$29 · Free shipping", font, w_all + 1) == ["$29 · Free shipping"]
      and textlayer.wrap("$29 · Free shipping", font, w_all - 20) == ["$29", "Free shipping"]
      and textlayer.wrap("$29 · Free shipping", font, font.getlength("shipping") + 2) == ["$29", "Free", "shipping"],
      '"$29 · Free shipping" stays on one line when it fits, and otherwise breaks at the dot, which is dropped')
_, rep = textlayer.render(Image.new("RGB", (896, 1120), (236, 210, 184)), "offer", WORDS, KIT, "4:5")
fb = textlayer.fit_text("$29 · Free shipping", "sans", "bold", 179, 132, 72, 26, max_lines=3, leading=1.08)
check(rep["layers"]["badge"]["fit"] and fb["fit"] and not any("·" in ln for ln in fb["lines"]) and fb["lines"][0] == "$29",
      f"in the offer badge it reads {fb['lines']}: no line is left holding the dot")
check(textlayer.wrap("Now only $29 today", font, font.getlength("Now only $29") - 4) == ["Now", "only $29", "today"]
      and textlayer.wrap("$29 off everything", font, font.getlength("$29 off everything") - 4) == ["$29 off", "everything"]
      and textlayer.wrap("Save 20% on mugs", font, font.getlength("Save 20% on") - 4)[0] == "Save 20%",
      "a price keeps the word beside it instead of ending or starting a line alone")
check(textlayer.wrap("Mug + lid today", font, font.getlength("Mug + lid") + 4) == ["Mug + lid", "today"]
      and textlayer.wrap("— Dana R.", font, 2000) == ["— Dana R."]
      and textlayer.wrap("well-made, leak-proof", font, 2000) == ["well-made, leak-proof"]
      and textlayer.wrap("The mug that keeps up with you", font, 330) == ["The mug that", "keeps up with", "you"]
      and textlayer.wrap("", font, 100) == [] and textlayer.wrap("·", font, 100) == [],
      'a "+" keeps both its neighbours, an opening dash stays on its word, hyphenated words and plain text wrap as before')
print(f"\n{passed} text layer fit checks passed")

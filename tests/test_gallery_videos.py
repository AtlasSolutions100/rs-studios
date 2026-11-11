"""The home page's gallery with video ads in it (public_site.showcase, static/showcase/manifest.json):

  * what ships: one grid of image ads and video ads; every file exists and is small enough for a phone; every tile
    has a title and alt text; there are presenter videos and videos with no presenter
  * the page: a video tile is a muted loop that loads nothing until it is near the screen; a person on screen is
    tagged "AI presenter"; a Sound button only on clips with speech; there is no separate presenter section
  * the loader: presenter and sound only for videos and only when the entry says true; an entry without a preset
    uses its own title
  * the FAQ answers the presenter-video question with today's numbers
  * pictures and clips go out with the right content type

    python tests/test_gallery_videos.py      (tests/test_app.py runs it too)
"""
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_gallery_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER", "RS_MARKUP"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import presets  # noqa: E402
import public_site  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


shipped = json.load(open(os.path.join(ROOT, "static", "showcase", "manifest.json"), encoding="utf-8"))
shots = public_site.showcase()
videos = [s for s in shots if s["kind"] == "video"]
images = [s for s in shots if s["kind"] == "image"]

print("== what ships")
check("videos" not in shipped and len(shots) == public_site.SHOWCASE_MAX == 16, "one list, and the grid shows sixteen tiles")
check(len(videos) >= 8 and len(images) >= 4, f"image ads and video ads share the grid ({len(images)} images, {len(videos)} videos)")
check(all(os.path.getsize(os.path.join(ROOT, "static", v["src"])) < 3_000_000 for v in videos), "each clip is under 3 MB")
check(all(v["poster"] and os.path.getsize(os.path.join(ROOT, "static", v["poster"])) < 120_000 for v in videos), "each clip has a small poster")
check(all(s["preset_name"] and len(s["alt"]) > 30 for s in shots), "every tile has a title and alt text")
check(sum(v["presenter"] for v in videos) >= 4 and sum(not v["presenter"] for v in videos) >= 4, "there are videos with a presenter and videos without")
check(all(v["sound"] for v in videos if v["presenter"]), "every presenter clip has its speech")
check(any(s["preset"] == "pov-unboxing" for s in videos), "the unboxing video is in")
order = [s["kind"] for s in shots]
check(order[0] == "video" and "image" in order[:4] and max(len(m.group()) for m in re.finditer(r"v+", "".join(k[0] for k in order))) <= 2,
      "videos and images alternate; the grid opens on a video")
banned = re.compile(r"\b(guarantee|proven|#1|best[- ]selling|review|customers? (say|love)|results?)\b", re.I)
check(not any(banned.search(s["caption"] + " " + s["alt"] + " " + s["preset_name"]) for s in shots), "no result claims or reviews in the words around them")

print("== the loader")
bad = os.path.join(TMP, "show")
os.makedirs(bad)
from PIL import Image  # noqa: E402
Image.new("RGB", (40, 50), "white").save(os.path.join(bad, "pic.webp"))
for n in ("ok.mp4", "note.txt"):
    open(os.path.join(bad, n), "wb").write(b"x")
entries = [
    {"file": "ok.mp4", "kind": "video", "poster": "pic.webp", "title": "Presenter video", "caption": "Desk talk", "alt": "Someone at a desk.",
     "presenter": True, "sound": True},
    {"file": "ok.mp4", "kind": "video", "poster": "pic.webp", "preset": "turn", "alt": "The product turning.", "presenter": "yes"},
    {"file": "pic.webp", "kind": "image", "preset": "hero", "alt": "A picture.", "presenter": True, "sound": True},
    {"file": "gone.mp4", "kind": "video", "title": "No file", "alt": "x"},
    {"file": "note.txt", "kind": "video", "title": "Not a video", "alt": "x"},
    {"file": "ok.mp4", "kind": "video", "title": "No words"},
]
json.dump({"items": entries}, open(os.path.join(bad, "manifest.json"), "w"))
real_dir = public_site.SHOWCASE_DIR
public_site.SHOWCASE_DIR = bad
public_site._showcase.update(key=None, items=[])
got = public_site.showcase()
check([g["preset_name"] for g in got] == ["Presenter video", presets.get("turn").name, presets.get("hero").name if presets.get("hero") else ""],
      "a preset gives the title; without one the entry's own title is used; broken entries are dropped")
check((got[0]["presenter"], got[0]["sound"]) == (True, True) and (got[1]["presenter"], got[1]["sound"]) == (False, False)
      and (got[2]["presenter"], got[2]["sound"]) == (False, False), "presenter and sound are for videos, and only when the entry says true")
public_site.SHOWCASE_DIR = real_dir
public_site._showcase.update(key=None, items=[])

print("== the page")
HTML = {"Accept": "text/html"}
old = appmod.app.test_client().get("/", headers=HTML).get_data(as_text=True)
check("data-sound" not in old and "lv-vid" not in old, "the old home page is unchanged")
os.environ["HOME_V2"] = "1"
home = appmod.app.test_client().get("/", headers=HTML).get_data(as_text=True)
os.environ.pop("HOME_V2")
check('<h2 id="lv-made">Made in Campaignify</h2>' in home and f'data-showcase="{len(shots)}"' in home and home.count('class="lv-shot"') == len(shots),
      "Made in Campaignify shows every tile")
check("lv-reel" not in home and "Every person here is an AI presenter" not in home and "our team made" not in home,
      "there is no separate presenter section and no explanation under it")
check(home.count('class="lv-clip-tag">AI presenter<') == sum(v["presenter"] for v in videos), "a person on screen is tagged AI presenter")
check(home.count("data-sound aria-pressed") == sum(v["sound"] for v in videos), "a Sound button only on clips with speech")
tiles = re.findall(r'<video class="a9x16" data-inview muted loop playsinline preload="none" data-poster="[^"]+" width="\d+" height="\d+" aria-label="[^"]{30,}">', home)
check(len(tiles) == len(videos), "clips are muted loops that load nothing until they are near the screen, each with its description")

print("== the FAQ")
faq = dict(public_site.home_faq())
r = catalog.rates()
a = faq.get("Can it make video ads with a presenter?", "")
check(f"{public_site.presenter_count()} AI presenters" in a and f"8-second presenter video is {r['talk8']} credits" in a, "the presenter-video answer quotes today's roster and price")
check("leads board" in faq["Can I see how my campaign is doing?"] and "leads board" in faq["Which plan do I need?"], "the leads board is in the answers about results and plans")
check("coffee" not in faq["Does it work for my product?"] and "Services and apps" in faq["Does it work for my product?"], "the product answer covers services and apps")
check(f"8-second presenter video {r['talk8']}" in faq["What does a credit buy?"], "the credit answer includes a presenter video")
check("{" not in "".join(faq.values()), "no unfilled number in any answer")

print("== pictures are served as pictures")
check(mimetypes.guess_type("x.webp")[0] == "image/webp" and mimetypes.guess_type("x.avif")[0] == "image/avif", "WebP and AVIF are named types on every Python version")
rr = appmod.app.test_client().get("/static/" + videos[0]["poster"])
check(rr.status_code == 200 and rr.headers["Content-Type"] == "image/webp", "a poster goes out as image/webp")
rr = appmod.app.test_client().get("/static/" + videos[0]["src"])
check(rr.status_code == 200 and rr.headers["Content-Type"] == "video/mp4", "a clip goes out as video/mp4")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

"""The reel of video ads on the new home page (public_site.showreel, static/showcase/manifest.json "videos"):

  * what ships: every clip and poster exists and is small enough for a phone, has a title, a caption and alt text,
    and says where it came from; a clip from our team is never labelled as made in Campaignify
  * the loader drops an entry with a missing file, a wrong kind, no source, an unknown preset or no alt text
  * the page: one tile per clip, people labelled "AI presenter", a Sound button only on clips with speech, clips
    muted until someone asks; the old home page has no reel

    python tests/test_showreel.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_showreel_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import presets  # noqa: E402
import public_site  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


SHOW = os.path.join(ROOT, "static", "showcase")
shipped = json.load(open(os.path.join(SHOW, "manifest.json"), encoding="utf-8"))["videos"]
reel = public_site.showreel()

print("== what ships")
check(len(reel) == len(shipped) >= 8 and len(reel) <= public_site.REEL_MAX, "every shipped clip is shown, and there are at least eight")
check(all(os.path.getsize(os.path.join(ROOT, "static", it["src"])) < 3_000_000 for it in reel), "each clip is under 3 MB")
check(all(os.path.getsize(os.path.join(ROOT, "static", it["poster"])) < 120_000 for it in reel), "each has a small poster")
check(all(it["title"] and it["caption"] and len(it["alt"]) > 30 for it in reel), "each has a title, a caption and alt text")
check({it["source"] for it in reel} == {"campaignify", "team"}, "both kinds are there: from Campaignify, and from our team")
check(all("Campaignify" not in it["caption"] or "our team" not in it["caption"] for it in reel)
      and all("Made in Campaignify" not in it["caption"] and it["caption"].startswith("Made by our team") for it in reel if it["source"] == "team"),
      "a clip our team made says so and is never labelled as made in Campaignify")
check(all(it["caption"].startswith("Made in Campaignify") and it["title"] == presets.get(s["preset"]).name
          for it, s in zip(reel, shipped) if it["source"] == "campaignify"),
      "a clip from Campaignify carries its preset's name")
check(sum(it["presenter"] for it in reel) >= 4 and all(it["sound"] for it in reel if it["presenter"]), "presenter clips are there, each with speech")
banned = re.compile(r"\b(guarantee|proven|#1|best[- ]selling|review|customers? (say|love)|results?)\b", re.I)
check(not any(banned.search(it["caption"] + " " + it["alt"] + " " + it["title"]) for it in reel), "no result claims or reviews in the words around them")

print("== the loader")
bad = os.path.join(TMP, "show")
os.makedirs(bad)
for n in ("ok.mp4", "ok.webp", "note.txt"):
    open(os.path.join(bad, n), "wb").write(b"x")
entries = [
    {"file": "ok.mp4", "poster": "ok.webp", "source": "team", "title": "Fine", "caption": "c", "alt": "A clip that is fine."},
    {"file": "gone.mp4", "poster": "ok.webp", "source": "team", "title": "No file", "alt": "x"},
    {"file": "ok.mp4", "poster": "gone.webp", "source": "team", "title": "No poster", "alt": "x"},
    {"file": "note.txt", "poster": "ok.webp", "source": "team", "title": "Not a video", "alt": "x"},
    {"file": "ok.mp4", "poster": "ok.mp4", "source": "team", "title": "Poster is a video", "alt": "x"},
    {"file": "ok.mp4", "poster": "ok.webp", "title": "No source", "alt": "x"},
    {"file": "ok.mp4", "poster": "ok.webp", "source": "stock", "title": "Stock", "alt": "x"},
    {"file": "ok.mp4", "poster": "ok.webp", "source": "campaignify", "preset": "no-such-preset", "alt": "x"},
    {"file": "ok.mp4", "poster": "ok.webp", "source": "team", "title": "No alt"},
    {"file": "../ok.mp4", "poster": "ok.webp", "source": "team", "title": "Outside", "alt": "x"},
    {"file": "ok.mp4", "poster": "ok.webp", "source": "campaignify", "preset": "turn", "alt": "The product turning.", "presenter": "yes"},
]
json.dump({"videos": entries}, open(os.path.join(bad, "manifest.json"), "w"))
real_dir = public_site.SHOWCASE_DIR
public_site.SHOWCASE_DIR = bad
public_site._reel.update(key=None, items=[])
got = public_site.showreel()
check([g["title"] for g in got] == ["Fine", presets.get("turn").name], "only the two sound entries survive")
check(got[1]["presenter"] is False and got[1]["sound"] is False, "presenter and sound are on only when the entry says true")
check(public_site.showreel(limit=1) == got[:1], "the limit is kept")
public_site.SHOWCASE_DIR = real_dir
public_site._reel.update(key=None, items=[])
public_site._showcase.update(key=None, items=[])

print("== the page")
HTML = {"Accept": "text/html"}
old = appmod.app.test_client().get("/", headers=HTML).get_data(as_text=True)
check("lv-reel" not in old and "data-sound" not in old, "the old home page has no reel")
os.environ["HOME_V2"] = "1"
home = appmod.app.test_client().get("/", headers=HTML).get_data(as_text=True)
os.environ.pop("HOME_V2")
check(f'data-reel="{len(reel)}"' in home and home.count('class="lv-clip"') == len(reel), "the new home page shows one tile per clip")
check(home.count('class="lv-clip-tag">AI presenter<') == sum(it["presenter"] for it in reel), "every person on screen is labelled AI presenter")
check(home.count("data-sound") - home.count("[data-sound]") == sum(it["sound"] for it in reel), "a Sound button only on clips with speech")
tiles = re.findall(r'<video data-inview muted loop playsinline preload="none" data-poster="[^"]+" width="720" height="1280" aria-label="[^"]{30,}">', home)
check(len(tiles) >= len(reel), "clips are muted loops that load nothing until they are near the screen, each with its description")
check("Every person here is an AI presenter." in home and "ads our team made with AI video" in home, "the block says who is AI and who made what")
check('<h2 id="lv-made">Made in Campaignify</h2>' in home and home.index("lv-reel-head") > home.index('id="lv-made"'),
      "the reel sits under the gallery of Campaignify's own outputs")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

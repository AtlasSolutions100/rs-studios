"""Gallery clips outside the home grid (Nov 12 2026, m40):

  * public_site.showcase_clip finds a gallery video by file name, in the whole manifest; a library-store clip only
    once it is on this server
  * app.page_clips: an answer-first page's hero takes site clips and gallery clips; one that isn't here yet is
    replaced by a site clip, so the hero keeps three
  * every clip key the pages name is a site clip or a video in the manifest
  * the home page's "Try-ons, app demos, unboxings" row: its text always, its clips once they are here
  * the Try-on preset has its example picture and clip

    python tests/test_page_clips.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_pageclips_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
os.environ["HOME_V2"] = "1"     # the home page in the new look, as deployed
for k in ("RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER", "RS_MARKUP", "LIB_STORE"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import libstore  # noqa: E402
import presets  # noqa: E402
import public_pages  # noqa: E402
import public_site  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


manifest = json.load(open(os.path.join(ROOT, "static", "showcase", "manifest.json"), encoding="utf-8"))
videos = {it["file"][:-4]: it for it in manifest["items"] if it["kind"] == "video"}
HTML = {"Accept": "text/html"}

print("== every clip a page names exists")
named = set()
for pages in (public_pages.SOLUTIONS, public_site.SOLUTIONS):
    for slug, sol in pages.items():
        for k in sol.get("clips", []):
            named.add(k)
            check(k in appmod.CLIP_BY_KEY or k in videos, f"{slug}: clip {k} is a site clip or a gallery video")
check(any(k in videos and "store" in videos[k] for k in named), "some pages play library-store clips")

print("== before the store clips arrive")
check(public_site.showcase_clip("ex2-web-app-demo.mp4") is None, "a store clip that isn't here isn't offered")
bundled = next(k for k, it in videos.items() if "store" not in it)
check(public_site.showcase_clip(bundled + ".mp4")["url"] == f"/static/showcase/{bundled}.mp4", "a bundled clip is")
with appmod.app.test_request_context():
    got = appmod.page_clips(["ex2-web-app-demo", "ugc-app", "ad_image"])
check(len(got) == 3 and [c["key"] for c in got] == ["ugc-app", "ad_image", "ugc-coffee"],
      "a missing clip is replaced by a site clip, so the hero keeps three")
c = public_pages.SOLUTIONS["ai-ads-for-saas"]
client = appmod.app.test_client()
body = client.get("/ai-ads-for-saas", headers=HTML).get_data(as_text=True)
hero = body.split('id="how"')[0]
check(hero.count("<video") == 3 and "/lib/" not in hero, "the SaaS page plays three site clips meanwhile")
home = client.get("/", headers=HTML).get_data(as_text=True)
check('id="k-formats"' in home and "Try-ons, app demos, unboxings" in home
      and "cfk-clips" not in home, "the home page's formats tile: its text, no clips yet")

print("== once they are here")
os.makedirs(libstore.folder(), exist_ok=True)
for it in manifest["items"]:
    if "store" in it:
        with open(os.path.join(libstore.folder(), it["file"]), "wb") as fh:
            fh.write(b"\0" * it["store"]["bytes"])
web = public_site.showcase_clip("ex2-web-app-demo.mp4")
check(web is not None and web["url"].startswith("/lib/ex2-web-app-demo.mp4?v=")
      and web["poster_url"] == "/static/showcase/ex2-web-app-demo.webp", "a store clip that arrived is offered, from /lib/")
body = client.get("/ai-ads-for-saas", headers=HTML).get_data(as_text=True)
hero = body.split('id="how"')[0]
check(hero.count("<video") == 3 and '<source src="/lib/ex2-web-app-demo.mp4?v=' in hero
      and 'poster="/static/showcase/ex2-web-app-demo.webp"' in hero, "the SaaS page leads with the web app demo")
body = client.get("/ai-ads-for-real-estate", headers=HTML).get_data(as_text=True)
hero = body.split('id="how"')[0]
check("/lib/ex-realtor-presenter.mp4" in hero and "lp-snd" in hero and "AI presenter" in hero,
      "the real estate page plays the agent clip, tagged and with a sound button")
home = client.get("/", headers=HTML).get_data(as_text=True)
row = re.search(r'<article[^>]*id="k-formats".*?</article>', home, re.S).group(0)
check(row.count("<video") == 3 and all(f in row for f in ("ex2-tryon-denim-jacket", "ex2-web-app-demo", "ex2-unboxing-pov-bottle"))
      and "data-inview" in row and 'preload="none"' in row and f"{len(presets.RECIPES)} video styles" in row,
      "the home page's formats row plays a try-on, a web app demo and an unboxing, loading nothing until near")

print("== the Try-on preset's example")
check(presets.example_url("try-on") == "/static/presets/try-on.webp" and presets.example_video_url("try-on") == "/static/presets/try-on.mp4"
      and os.path.getsize(os.path.join(ROOT, "static", "presets", "try-on.mp4")) < 600_000, "Try-on has its picture and clip")

print(f"\npage clips: {passed} checks passed")

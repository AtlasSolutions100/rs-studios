"""The public pages' answer to "does Campaignify post or run my ads" follows the AD_LAUNCH flag (public_site.runs):

  * while launching into ad accounts is not open to every workspace (AD_LAUNCH unset, off or beta, or on with no Meta
    app set up) the pages say you launch in the ads manager and that launching from Campaignify is coming, starting
    with Meta; they never answer with a flat "No." and never claim the app posts or runs ads
  * with AD_LAUNCH on and the Meta app set up they say yes, on Meta, switched off until you turn it on
  * every page that asks the question is filled in (no placeholder left in the page), in both states

    python tests/test_faq_launch.py      (tests/test_app.py runs it too)
"""
import html
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_faqlaunch_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test", HOME_V2="1")
for k in ("UI_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER", "RS_MARKUP", "AD_LAUNCH",
          "AD_LAUNCH_WORKSPACES", "META_APP_ID", "META_APP_SECRET"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import public_site  # noqa: E402
from adplatforms import meta  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    passed += 1


c = appmod.app.test_client()
PAGES = ["/", "/pricing", "/ai-marketing-platform-for-small-business", "/creatify-alternative", "/ai-facebook-ad-generator",
         "/ai-ads-for-coaches"]


def text(path):
    r = c.get(path)
    check(r.status_code == 200, f"{path} opens")
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.get_data(as_text=True))))


def answer(path="/"):
    t = text(path)
    return t.split("Does Campaignify post or run my ads?", 1)[1][:900] if "Does Campaignify post or run my ads?" in t else ""


# ---------------------------------------------------------------- not open to everyone yet
check(public_site.launch_live() is False, "with no AD_LAUNCH, launching is not live")
home = answer("/")
check("You put them live, and Campaignify does the rest." in home,
      "the home page answers with what Campaignify does, not a flat No")
check("coming soon, starting with Meta" in home and "Your ad money stays in your own ad account" in home,
      "it says launching from Campaignify is coming, starting with Meta, and where the spend stays")
for path in PAGES:
    t = text(path)
    check("{runs" not in t and "{limits_run" not in t and "{results" not in t, f"{path}: every placeholder is filled in")
    check("Does Campaignify post or run my ads? No." not in t and "No. It plans the campaign" not in t,
          f"{path}: no flat No")
    check(not re.search(r"\b(we|campaignify|it) (post|posts|run|runs|publish|publishes) (your|the) ads\b", t, re.I),
          f"{path}: never claims the app posts or runs ads")
check("coming soon, starting with Meta" in text("/ai-marketing-platform-for-small-business"), "the small business page says it is coming")
check("coming soon, starting with Meta" in text("/creatify-alternative"), "the Creatify page's limits say it is coming")
check("coming soon, starting with Meta" in text("/ai-facebook-ad-generator"), "the Facebook page says it is coming")
check("stays in that app's ads manager" in text("/"), "results from the ad platforms: not yet, said plainly")

for value in ("off", "beta"):
    os.environ["AD_LAUNCH"] = value
    check(public_site.launch_live() is False and "coming soon" in answer("/"), f"AD_LAUNCH={value}: still coming soon")
os.environ["AD_LAUNCH"] = "on"
check(public_site.launch_live() is False and "coming soon" in answer("/"),
      "AD_LAUNCH=on with no Meta app set up: still coming soon")

# ---------------------------------------------------------------- open to everyone
real = meta.ADAPTER.configured
meta.ADAPTER.configured = lambda: True
try:
    check(public_site.launch_live() is True, "AD_LAUNCH=on and the Meta app set up: launching is live")
    home = answer("/")
    check("Yes, on Meta to start." in home and "switched off" in home and "coming soon" not in home,
          "the home page says yes, on Meta, created switched off")
    check("On a paid plan" in home and "Your ad money stays in your own ad account" in home,
          "it names the paid plan and where the spend stays")
    for path in PAGES:
        t = text(path)
        check("{runs" not in t and "{limits_run" not in t and "{results" not in t, f"{path}: filled in when live too")
    check("show how they are doing here too" in text("/"), "results: launched campaigns report here")
    check("On Meta, yes" in text("/ai-facebook-ad-generator"), "the Facebook page says yes")
finally:
    meta.ADAPTER.configured = real
    os.environ.pop("AD_LAUNCH", None)
check("coming soon" in answer("/"), "and back to coming soon when the flag goes")

shutil.rmtree(TMP, ignore_errors=True)
print(f"test_faq_launch: {passed} checks passed")

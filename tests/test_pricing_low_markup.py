"""Generation priced near cost (RS_MARKUP=1.08, RS_HD_MARKUP=1.08; owner decision Nov 11 2026):

  * what things cost in credits: an image ad 12, an 8 second talking video 400, a 5 second product video 250
  * a plan's credits go about twice as far: Starter makes 5 talking videos, Growth 15, Scale 45
  * the credit packs are cut so none sells below what its credits cost us; the dollar prices are unchanged
  * the free start is still two image ads' worth
  * the default markup (2.0) keeps the old packs: checked in a second process
  * Stripe: a pack's product description is brought up to date when the packs are re-cut, so Checkout never promises
    the old number; a product that isn't ours is left alone

    python tests/test_pricing_low_markup.py      (tests/test_app.py runs it too)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_lowmarkup_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  RS_MARKUP="1.08", RS_HD_MARKUP="1.08")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
          "STRIPE_SETUP", "MCP_SERVER", "RS_STARTER_CREDITS", "RS_LIVE_IDS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import catalog  # noqa: E402
import plans  # noqa: E402
import stripe_setup  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


print("== what things cost")
r = catalog.rates()
check((r["image"], r["talk8"], r["video5"]) == (12, 400, 250), "an image ad is 12 credits, an 8 s talking video 400, a 5 s product video 250")
check(r["talk10_1080"] == catalog.price_credits("ugc_video", duration=10, resolution="1080p") and r["talk10_1080"] < 1300,
      "1080p carries the same small markup")
check([plans.PLANS[c]["credits"] // r["talk8"] for c in ("starter", "growth", "scale")] == [5, 15, 45],
      "a plan makes 5, 15 or 45 talking videos a month (it was 2, 8 and 24)")
check(catalog.starter_credits() == 2 * r["image"] == 24, "the free start is still two image ads")

print("== the packs")
check([(p["code"], p["usd"], p["credits"], p["bonus"]) for p in catalog.PACKS]
      == [("starter", 29, 1500, 0), ("pro", 79, 4800, 700), ("studio", 199, 14400, 4100)], "the packs are 1,500, 4,800 and 14,400 credits at the same dollar prices")
margins = [round(catalog.pack_margin(p), 2) for p in catalog.PACKS]
check(all(m >= 0.25 for m in margins) and margins == sorted(margins, reverse=True), f"no pack sells near or below cost (margins after Stripe's fee: {margins})")
plan_margins = [round((p["usd"] * 0.971 - 0.30 - p["credits"] * catalog.credit_cost_usd()) / p["usd"], 2) for p in plans.PAID]
check(all(m >= 0.25 for m in plan_margins), f"no plan sells near or below cost if every credit is spent ({plan_margins})")
old = dict(starter=2900, pro=8500, studio=23000)
check(any(p["usd"] * 0.971 - 0.30 < old[p["code"]] * catalog.credit_cost_usd() for p in catalog.PACKS),
      "(the old sizes would have sold a pack below cost at this markup, which is why they are cut)")
per_dollar = [p["credits"] / p["usd"] for p in catalog.PACKS]
check(per_dollar == sorted(per_dollar), "a bigger pack is still the better deal per dollar")

print("== the pages show today's numbers")
c = appmod.app.test_client()
html = c.get("/pricing").get_data(as_text=True)
check("1,500 credits" in html and "14,400 credits" in html and "2,900 credits" not in html and "23,000" not in html, "the pricing page shows the new pack sizes and none of the old")

cl = c.get("/mcp", headers={"Accept": "text/html"}).get_data(as_text=True)
check("2 image ads at 12 credits each, 24 credits in all" in cl, "the Claude page's example quotes today's price")

print("== the default markup keeps the old packs")
env = dict(os.environ, RS_DATA=tempfile.mkdtemp(prefix="rs_defmarkup_", dir=TMP))
env.pop("RS_MARKUP"), env.pop("RS_HD_MARKUP")
out = subprocess.run([sys.executable, "-c", "import catalog; print([p['credits'] for p in catalog.PACKS], catalog.rates()['image'], "
                      "all(catalog.pack_margin(p) > 0.25 for p in catalog.PACKS))"], cwd=ROOT, env=env, capture_output=True, text=True)
check(out.stdout.strip().endswith("[2900, 8500, 23000] 22 True"), f"at 2.0 the packs are 2,900, 8,500 and 23,000 and an image is 22 credits ({out.stdout.strip()[-60:]})")

print("== Stripe says what the member gets")
check(stripe_setup.pack_about(catalog.PACK_BY_CODE["pro"]) == "4,800 Campaignify credits (4,100 + 700 bonus) for making ads at campaignify.ai. One-time purchase."
      and stripe_setup.pack_about(catalog.PACK_BY_CODE["starter"]) == "1,500 Campaignify credits for making ads at campaignify.ai. One-time purchase.",
      "a pack's description states its credits")
w = stripe_setup.wanted()
check(w["about:pack:studio"].startswith("14,400") and w["pack:studio"] == 19900, "a re-cut changes what the ids are settled for, so the next start looks again")
products = {
    "prod_pro": {"id": "prod_pro", "active": True, "metadata": {"brand": "Campaignify", "pack": "pro"},
                 "description": "8,500 Campaignify credits (7,900 + 600 bonus) for making ads at campaignify.ai. One-time purchase."},
    "prod_right": {"id": "prod_right", "active": True, "metadata": {"brand": "Campaignify", "pack": "starter"},
                   "description": stripe_setup.pack_about(catalog.PACK_BY_CODE["starter"])},
    "prod_other": {"id": "prod_other", "active": True, "metadata": {"brand": "The Morning Upgrade"}, "description": "Coffee."},
}
posts = []


def fake_get(path, params=None):
    pid = path.rsplit("/", 1)[-1]
    if path.startswith("/products/") and pid in products:
        return products[pid]
    raise billing.BillingError("Stripe 404: No such product", 404, "resource_missing", "id")


def fake_post(path, form, idempotency_key=None):
    posts.append((path, dict(form)))
    products[path.rsplit("/", 1)[-1]]["description"] = dict(form)["description"]
    return products[path.rsplit("/", 1)[-1]]


billing._get, billing._post = fake_get, fake_post
stripe_setup._describe("prod_pro", stripe_setup.pack_about(catalog.PACK_BY_CODE["pro"]))
check(posts == [("/products/prod_pro", {"description": "4,800 Campaignify credits (4,100 + 700 bonus) for making ads at campaignify.ai. One-time purchase."})],
      "an out-of-date description on our own product is corrected, and only the description is sent")
stripe_setup._describe("prod_right", stripe_setup.pack_about(catalog.PACK_BY_CODE["starter"]))
stripe_setup._describe("prod_other", "anything")
stripe_setup._describe("prod_gone", "anything")
stripe_setup._describe("", "anything")
check(len(posts) == 1, "a description that is right, another brand's product and a missing product are left alone")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")

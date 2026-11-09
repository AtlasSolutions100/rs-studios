"""Live ids are settings (roadmap M7.4, observation 10; SEC-9): liveids.py is the one place they live.

    python tests/test_liveids.py      (tests/test_app.py runs it too)

  * every module reads its live id through liveids.get: a setting wins, else the production value
  * RS_LIVE_IDS=settings: nothing falls back to production (a fork or a test-mode Stripe account)
  * no live id or owner email is written anywhere else in the source
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_liveids_")

import liveids  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        shutil.rmtree(TMP, True)
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


PROBE = r"""
import json, sys
sys.path.insert(0, %r)
import plans, catalog, billing, gads, google_auth, public_site, messaging, owners, liveids
print(json.dumps({"plan": plans.PLANS["growth"]["price_id"], "product": plans.PLANS["scale"]["product_id"],
                  "pack": catalog.PACK_BY_CODE["pro"]["price_id"], "portal": billing.PORTAL_CONFIG, "tag": gads.tag_id(),
                  "client": google_auth.client_id(), "indexnow": public_site.INDEXNOW_KEY,
                  "address": messaging.CAMPAIGNIFY_ADDRESS, "owners": owners.table(),
                  "free": plans.PLANS["free"]["price_id"], "report": liveids.report()}))
""" % ROOT


def probe(**env):
    e = {k: v for k, v in os.environ.items() if k not in liveids.PRODUCTION and k not in ("RS_LIVE_IDS", "RS_OWNER_INVITES")}
    e.update(RS_DATA=TMP, RS_WORKER="0", **env)
    out = subprocess.run([sys.executable, "-c", PROBE], env=e, capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


P = liveids.PRODUCTION
d = probe()
check(d["plan"] == P["STRIPE_PLAN_PRICE_GROWTH"] and d["product"] == P["STRIPE_PLAN_PRODUCT_SCALE"]
      and d["pack"] == P["STRIPE_PRICE_PRO"] and d["portal"] == P["STRIPE_PORTAL_CONFIG"] and d["tag"] == P["GADS_TAG_ID"]
      and d["client"] == P["GOOGLE_OAUTH_CLIENT_ID"] and d["indexnow"] == P["INDEXNOW_KEY"]
      and d["address"] == P["RS_POSTAL_ADDRESS"] and d["free"] is None,
      "with no settings every module uses the production values (a server without them works as before)")
check(all(src == "production default" for _, src in d["report"]), "and the report says where each came from")
check(d["owners"] == {}, "no owner setup links unless RS_OWNER_INVITES is set (no owner email in the source)")

d = probe(STRIPE_PLAN_PRICE_GROWTH="price_test_growth", STRIPE_PRICE_PRO="price_test_pro", STRIPE_PORTAL_CONFIG="bpc_test",
          GADS_TAG_ID="AW-1", GOOGLE_OAUTH_CLIENT_ID="test.apps.googleusercontent.com", INDEXNOW_KEY="a" * 32,
          RS_POSTAL_ADDRESS="1 Test St", STRIPE_PLAN_PRODUCT_SCALE="prod_test",
          RS_OWNER_INVITES="owner@example.com:" + "0" * 64)
check(d["plan"] == "price_test_growth" and d["pack"] == "price_test_pro" and d["portal"] == "bpc_test" and d["tag"] == "AW-1"
      and d["client"] == "test.apps.googleusercontent.com" and d["indexnow"] == "a" * 32 and d["address"] == "1 Test St"
      and d["product"] == "prod_test", "a setting wins over the production value, for every id")
check(d["owners"] == {"0" * 64: "owner@example.com"}, "owner setup links come from RS_OWNER_INVITES")
check(dict(d["report"])["STRIPE_PRICE_PRO"] == "setting" and dict(d["report"])["STRIPE_PRICE_STARTER"] == "production default",
      "the report marks settings and defaults apart")

d = probe(RS_LIVE_IDS="settings", STRIPE_PRICE_PRO="price_test_pro")
check(d["plan"] is None and d["portal"] is None and d["tag"] == "" and d["client"] == "" and d["address"] == ""
      and d["pack"] == "price_test_pro", "RS_LIVE_IDS=settings: nothing falls back to the production accounts")
check(dict(d["report"])["STRIPE_PRICE_STARTER"] == "missing", "and the report says missing")

# no live id or owner email anywhere else in the source
values = [v for k, v in P.items() if k != "RS_POSTAL_ADDRESS"] + ["Gould St", "@rectorandstull.com",
                                                                   "ef32ab7259dbb618", "7eab982314d92a83"]
hits = []
for base, _, files in os.walk(ROOT):
    if any(part in base for part in (os.sep + ".git", os.sep + "tests", os.sep + "static", os.sep + "node_modules")):
        continue
    for f in files:
        # settings.example.txt documents the settings; the legal pages print the company address on purpose
        if not f.endswith((".py", ".html", ".js", ".txt", ".yml")) or f in ("liveids.py", "settings.example.txt"):
            continue
        p = os.path.join(base, f)
        try:
            src = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for v in values:
            if v in src and not (v == "Gould St" and not f.endswith(".py")):
                hits.append(f"{os.path.relpath(p, ROOT)}: {v[:24]}")
check(not hits, "no live id or owner email outside liveids.py: " + "; ".join(hits[:8]))
check(not re.search(r"koby@|travis@", open(os.path.join(ROOT, "owners.py")).read() + open(os.path.join(ROOT, "db.py")).read()),
      "owners.py and db.py hold no owner email (SEC-9)")

shutil.rmtree(TMP, True)
print(f"\nall {passed} checks passed")

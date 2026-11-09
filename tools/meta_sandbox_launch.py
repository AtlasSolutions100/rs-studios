"""The M2.3 launch on a real Meta ad account (a sandbox or test account): the acceptance run the fake Graph stands
in for until the Meta app exists. Run it on the server or a laptop, never in CI; it needs no database.

    export META_APP_ID=... META_APP_SECRET=...            # the Campaignify app (appsecret_proof on every call)
    export META_SANDBOX_TOKEN=...                         # a user or system-user token with ads_management,
                                                          # pages_manage_ads, pages_read_engagement (Graph API Explorer)
    python tools/meta_sandbox_launch.py --account act_123 --page 456 --image ad.png --video ad.mp4 \\
        [--pixel 789] [--ig 1784...] [--fail-at adset] [--record tests/fixtures/meta/recorded] [--keep]

What it does, with the same adapter code the worker runs (adplatforms/meta.py):
  1. a 2-ad launch (1 image, 1 video): uploads, 2 creatives, 1 campaign, 1 ad set, 2 ads, all PAUSED;
  2. checks Meta has exactly those objects (by the cfy tag) and that they are PAUSED;
  3. runs every step again with the lookup on: nothing new may be created;
  4. with --fail-at <step>, breaks that step (an invalid field) and checks the compensation (ads and creatives
     deleted, the campaign and ad set left PAUSED);
  5. cleans up: deletes the ads, creatives, ad set and campaign it made (unless --keep);
  6. with --record, writes every answer Meta gave (tokens and ids scrubbed) as JSON fixtures, to replace the
     hand-written tests/fixtures/meta/launch_*.json.

Exit 0 when every check passed. Nothing here can spend: everything is PAUSED and then deleted.
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RS_DATA", os.path.join(os.environ.get("TMPDIR", "/tmp"), "cfy_sandbox_launch"))

import adhttp  # noqa: E402
import adspec  # noqa: E402
import db  # noqa: E402
from adplatforms import meta  # noqa: E402


class Recorder:
    def __init__(self, inner, folder, secrets):
        self.inner, self.folder, self.secrets, self.n = inner, folder, secrets, 0

    def __call__(self, method, url, **kw):
        r = self.inner(method, url, **kw)
        if self.folder:
            self.n += 1
            path = re.sub(r"\d{6,}", "N", url.split("/v", 1)[-1].split("/", 1)[-1]).replace("/", "_")
            body = json.loads(adhttp.scrub(json.dumps(r.body), self.secrets)) if r.body is not None else None
            with open(os.path.join(self.folder, f"{self.n:02d}_{method}_{path}.json"), "w") as f:
                json.dump({"status": r.status, "body": body}, f, indent=1)
        return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--page", required=True)
    ap.add_argument("--ig")
    ap.add_argument("--pixel")
    ap.add_argument("--image", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--landing", default="https://campaignify.ai/")
    ap.add_argument("--fail-at", choices=["creative:1", "creative:2", "campaign", "adset", "ad:1", "ad:2"])
    ap.add_argument("--record")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    token = os.environ.get("META_SANDBOX_TOKEN") or ""
    if not (token and meta.app_id() and meta.app_secret()):
        sys.exit("Set META_SANDBOX_TOKEN, META_APP_ID and META_APP_SECRET.")
    db.init()                                 # adhttp's rate budget lives in a throwaway SQLite file
    if a.record:
        os.makedirs(a.record, exist_ok=True)
    adhttp.TRANSPORT = Recorder(adhttp._requests_transport, a.record, [token, meta.app_secret()])
    if not os.path.exists(a.video + ".jpg"):
        sys.exit(f"Put a poster frame next to the video: {a.video}.jpg")
    spec = adspec.CampaignSpec(
        objective="sales" if a.pixel else "traffic", platform="meta", name=f"Campaignify sandbox {int(time.time())}",
        daily_budget_minor=500, currency="USD", geo={"countries": ["US"]}, audience={"age_min": 18, "age_max": 65},
        conversion={"pixel_id": a.pixel, "event": "Purchase", "landing_url": a.landing} if a.pixel else None,
        creatives=[adspec.CreativeSpec(kind="image", asset_id=1, name="Image", primary_text="Sandbox test.",
                                       headline="Sandbox", cta="LEARN_MORE", destination_url=a.landing, aspect="4:5"),
                   adspec.CreativeSpec(kind="video", asset_id=2, name="Video", primary_text="Sandbox test.",
                                       headline="Sandbox", cta="LEARN_MORE", destination_url=a.landing, aspect="9:16")])
    row = {"page_id": a.page, "instagram_actor_id": a.ig, "timezone": None}
    assets = {1: {"kind": "image", "path": a.image}, 2: {"kind": "video", "path": a.video, "poster": a.video + ".jpg"}}
    steps = meta.plan_steps(spec, row, assets)
    lid = int(time.time()) % 100000000
    ctx = {"launch_id": lid, "account": a.account, "token": token, "results": {}, "max_wait": 120}
    done, failed = [], None
    for s in steps:
        if s["key"] == a.fail_at:
            s["request"]["status"] = "NOT_A_STATUS"
        for _ in range(60):
            try:
                ctx["results"][s["key"]] = r = meta.execute_step(s, dict(ctx, lookup=False))
                break
            except meta.NotReady:
                time.sleep(10)
            except adhttp.RateLimited as e:
                time.sleep(min(300, e.retry_after or 30))
            except adhttp.AdHttpError as e:
                failed = (s, e)
                break
        if failed:
            break
        s["external_id"] = r["id"]
        done.append(s)
        print(f"  made {s['key']}: {r['id']}")
    ok = True
    if failed:
        s, e = failed
        print(f"  {s['key']} failed as asked: code {e.code} subcode {e.subcode}: {e.user_message or e}")
        for d in reversed(done):
            print(f"  compensate {d['key']}: {meta.compensate(d, ctx)}")
        left = [k for k in ("creative", "ad") for i in (1, 2) if meta.find_tagged(k, dict(ctx), f"{k}:{i}")]
        ok = not left and a.fail_at is not None
        print("  compensation " + ("left nothing behind" if ok else f"left {left}"))
    else:
        for k in ("creative", "campaign", "adset", "ad"):
            for key in [s["key"] for s in steps if s["kind"] == k]:
                found = meta.find_tagged(k, dict(ctx), key)
                good = found and str(found["id"]) == ctx["results"][key]["id"] and \
                    str(found.get("effective_status") or "PAUSED") in ("PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED")
                ok = ok and bool(good)
                print(f"  {key}: {'found, PAUSED' if good else 'MISSING or not paused'}")
        for s in steps:
            if s["kind"] != "upload":
                r = meta.execute_step(s, dict(ctx, lookup=True))
                ok = ok and r.get("found") and r["id"] == s["external_id"]
        print("  a second run found every object and made nothing new" if ok else "  the second run made something new")
        if not a.keep:
            for s in reversed(done):
                if s["kind"] in ("ad", "creative", "adset", "campaign") and s.get("external_id"):
                    meta._delete(s["external_id"], token, a.account, max_wait=120)
            print("  cleaned up")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

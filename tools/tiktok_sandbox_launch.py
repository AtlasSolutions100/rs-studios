"""The M6.2 launch on TikTok's sandbox advertiser: the acceptance run the fake (tests/faketiktok.py) stands in for. Run
it on the server or a laptop, never in CI; it needs no app database.

    export TIKTOK_APP_ID=... TIKTOK_APP_SECRET=...      # the TikTok for Business developer app
    export TIKTOK_SANDBOX_TOKEN=...                     # the sandbox advertiser's access token (developer portal >
                                                        # the app > Sandbox), or a token from a Campaignify connect
    python tools/tiktok_sandbox_launch.py --advertiser <id> --video ad916.mp4 --poster ad916.jpg \\
        [--identity <custom identity id> | --identity-name "Campaignify sandbox"] [--pixel <pixel code> --objective sales] \\
        [--landing https://campaignify.ai/] [--fail-at ad:1] [--record tests/fixtures/tiktok/recorded] [--keep]
    python tools/tiktok_sandbox_launch.py --advertiser 1 --video a.mp4 --poster a.jpg --dry-run    # no network

What it does, with the adapter code the worker runs (adplatforms/tiktok.py):
  1. makes (or uses) a custom identity, uploads the video and its cover, then the campaign turned off, the ad group
     ($20 a day) and the ad;
  2. reads them back: the campaign is off (DISABLE), the ad group and ad exist;
  3. runs the campaign step again with the lookup on: it must find the campaign and make no second one;
  4. with --fail-at <step>, breaks that step and compensates: ads deleted, the campaign kept off;
  5. reads the report for the last 2 days (zeros on a new campaign);
  6. cleans up unless --keep: deletes the ad, the ad group and the campaign (the video stays in the library);
  7. with --record, writes every answer TikTok gave (tokens scrubbed) as JSON, to compare with or replace the
     hand-written tests/fixtures/tiktok/*.json, and to confirm the points marked "sandbox:" in the adapter (location
     ids, age bands, optimization events, schedule time zone, the video-ready field, error codes).

Exit 0 when every check passed. The sandbox advertiser never serves or spends.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RS_DATA", os.path.join(os.environ.get("TMPDIR", tempfile.gettempdir()), "cfy_tiktok_sandbox"))

import adhttp  # noqa: E402
import adspec  # noqa: E402
import db  # noqa: E402
from adplatforms import tiktok  # noqa: E402
from adplatforms.meta import NotReady  # noqa: E402


class Recorder:
    def __init__(self, inner, folder, secrets):
        self.inner, self.folder, self.secrets, self.n = inner, folder, secrets, 0

    def __call__(self, method, url, **kw):
        r = self.inner(method, url, **kw)
        if self.folder:
            self.n += 1
            path = re.sub(r"\d{8,}", "N", url.split("/open_api/v1.3/", 1)[-1].split("?")[0]).strip("/").replace("/", "_")
            body = json.loads(adhttp.scrub(json.dumps(r.body), self.secrets)) if r.body is not None else None
            with open(os.path.join(self.folder, f"{self.n:02d}_{method}_{path}.json"), "w") as f:
                json.dump({"status": r.status, "body": body}, f, indent=1)
        return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--advertiser", required=True)
    ap.add_argument("--video", required=True, help="a 9:16 MP4")
    ap.add_argument("--poster", required=True, help="its cover picture (JPEG)")
    ap.add_argument("--identity", help="an existing custom identity id")
    ap.add_argument("--identity-name", default="Campaignify sandbox")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--timezone", default="America/Los_Angeles")
    ap.add_argument("--budget", type=float, default=20.0, help="daily budget in the account's currency (TikTok's minimum 20)")
    ap.add_argument("--objective", choices=adspec.OBJECTIVES, default="traffic")
    ap.add_argument("--pixel", help="a pixel code, for --objective sales or leads")
    ap.add_argument("--landing", default="https://campaignify.ai/")
    ap.add_argument("--fail-at", help="campaign, adset or ad:1")
    ap.add_argument("--record")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cr = adspec.CreativeSpec(kind="video", asset_id=1, name="Sandbox video", primary_text="Campaignify sandbox test ad",
                             cta="LEARN_MORE", destination_url=a.landing, utm=tiktok.UTM, aspect="9:16")
    spec = adspec.CampaignSpec(objective=a.objective, platform="tiktok", name=f"Campaignify sandbox {int(time.time())}",
                               daily_budget_minor=int(a.budget * adspec.minor_factor(a.currency)), currency=a.currency,
                               start_at=(date.today() + timedelta(days=1)).isoformat(), geo={"countries": ["US"]},
                               audience={"age_min": 18, "age_max": 65, "genders": ["all"]}, creatives=[cr],
                               conversion={"pixel_id": a.pixel, "event": "Purchase"} if a.pixel else None)
    issues = [i for i in adspec.validate(spec) if i.level == "block"]
    if issues:
        sys.exit("The test spec doesn't validate: " + "; ".join(i.message for i in issues))
    row = {"external_account_id": a.advertiser, "page_id": a.identity or "IDENTITY", "timezone": a.timezone}
    steps = tiktok.plan_steps(spec, row, {1: {"kind": "video", "path": a.video, "poster": a.poster}})
    if a.dry_run:
        print(json.dumps(steps, indent=1))
        print(f"DRY RUN: {len(steps)} steps")
        sys.exit(0)
    token = os.environ.get("TIKTOK_SANDBOX_TOKEN") or ""
    if not (token and tiktok.app_id() and tiktok.app_secret()):
        sys.exit("Set TIKTOK_SANDBOX_TOKEN, TIKTOK_APP_ID and TIKTOK_APP_SECRET.")
    db.init()                                 # adhttp's rate budget lives in a throwaway SQLite file
    if a.record:
        os.makedirs(a.record, exist_ok=True)
    adhttp.TRANSPORT = Recorder(adhttp._requests_transport, a.record, [token, tiktok.app_secret()])
    if not a.identity:
        ident = tiktok.ADAPTER.create_identity(token, a.advertiser, a.identity_name)
        print(f"  made identity {ident['id']}")
        for s in steps:
            if s["kind"] == "ad":
                s["request"]["creative"]["identity_id"] = ident["id"]
    ctx = {"launch_id": int(time.time()) % 100000000, "account": a.advertiser, "token": token, "results": {},
           "max_wait": 120, "currency": a.currency}
    done, failed = [], None
    for s in steps:
        if s["key"] == a.fail_at:
            (s["request"].get("creative") or s["request"])["call_to_action" if s["kind"] == "ad" else "objective_type"] = "NOPE"
        for _ in range(80):
            try:
                ctx["results"][s["key"]] = r = tiktok.execute_step(s, dict(ctx, lookup=False))
                break
            except NotReady:
                time.sleep(15)
            except adhttp.RateLimited as e:
                time.sleep(min(300, e.retry_after or 30))
            except (adhttp.AdHttpError, KeyError, OSError) as e:
                failed = (s, e)
                break
        if failed:
            break
        s["external_id"], s["result"] = r.get("id"), r
        done.append(s)
        print(f"  made {s['key']}: {r.get('id')}")
    ok = True
    if failed:
        s, e = failed
        print(f"  {s['key']} failed as asked: {getattr(e, 'code', '')} {getattr(e, 'user_message', '') or e}")
        ok = a.fail_at is not None
        for d in reversed(done):
            print(f"  compensate {d['key']}: {tiktok.compensate(d, ctx)}")
    else:
        camp = next(s for s in steps if s["key"] == "campaign")
        obj = tiktok._read("campaign", camp["external_id"], ctx)
        good = bool(obj) and str(obj.get("operation_status")) == "DISABLE"
        print(f"  campaign: {'there, turned off' if good else 'MISSING or on'}")
        again = tiktok.execute_step(camp, dict(ctx, lookup=True))
        ok = good and bool(again.get("found")) and again["id"] == camp["external_id"]
        print("  a second run found the campaign and made no second one" if again.get("found") else
              "  the second run made something new")
        rows = tiktok.pull_insights(ctx, [{"kind": "campaign", "external_id": camp["external_id"]}],
                                    (date.today() - timedelta(days=1), date.today()))
        print(f"  report: {len(rows)} daily rows")
    if not a.keep:
        for s in reversed(done):
            if s["kind"] in ("ad", "adgroup", "campaign") and s.get("external_id"):
                try:
                    tiktok._status(s["kind"], s["external_id"], "DELETE", ctx)
                    print(f"  deleted {s['key']}")
                except adhttp.AdHttpError as e:
                    print(f"  couldn't delete {s['key']}: {e}")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

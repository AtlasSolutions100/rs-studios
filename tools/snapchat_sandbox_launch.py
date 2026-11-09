"""The M6.1 launch on a real Snapchat ad account: the acceptance run the fake (tests/fakesnap.py) stands in for. Run it on
the server or a laptop, never in CI; it needs no app database.

    export SNAPCHAT_CLIENT_ID=... SNAPCHAT_CLIENT_SECRET=...     # the Marketing API OAuth app (Business Manager)
    export SNAPCHAT_SANDBOX_REFRESH_TOKEN=...                    # a refresh token for a login on the test ad account
    python tools/snapchat_sandbox_launch.py --account <ad account id> --profile <public profile id> --video ad916.mp4 \\
        [--pixel <pixel id> --objective sales] [--landing https://campaignify.ai/] [--fail-at ad:1] \\
        [--record tests/fixtures/snapchat/recorded] [--keep]
    python tools/snapchat_sandbox_launch.py --account x --profile y --video ad.mp4 --dry-run     # no network: the calls

Getting the refresh token once: connect the ad account in Campaignify (Settings > Ad accounts > Connect Snapchat) on a
server with SNAPCHAT_ADS=1, or run Snap's OAuth by hand with the app (scope snapchat-marketing-api) and exchange the
code at https://accounts.snapchat.com/login/oauth2/access_token.

What it does, with the adapter code the worker runs (adplatforms/snapchat.py):
  1. uploads the 9:16 video, then makes the creative, a PAUSED campaign, a PAUSED ad squad ($5 a day) and a PAUSED ad;
  2. reads them back: each is there and PAUSED;
  3. runs the campaign step again with the lookup on: it must find the campaign and make no second one;
  4. with --fail-at <step>, breaks that step (a field Snap refuses) and compensates: ads deleted, the rest kept PAUSED;
  5. reads the campaign's stats for the last 2 days (zeros on a new campaign);
  6. cleans up unless --keep: deletes the ad, the ad squad and the campaign (creatives and media stay in the library);
  7. with --record, writes every answer Snap gave (tokens scrubbed) as JSON, to compare with or replace the
     hand-written tests/fixtures/snapchat/*.json, and to confirm the points marked "sandbox:" in the adapter.

Exit 0 when every check passed. Nothing is turned on: every object is PAUSED from the start.
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
os.environ.setdefault("RS_DATA", os.path.join(os.environ.get("TMPDIR", tempfile.gettempdir()), "cfy_snap_sandbox"))

import adhttp  # noqa: E402
import adspec  # noqa: E402
import db  # noqa: E402
from adplatforms import snapchat  # noqa: E402
from adplatforms.meta import NotReady  # noqa: E402


class Recorder:
    def __init__(self, inner, folder, secrets):
        self.inner, self.folder, self.secrets, self.n = inner, folder, secrets, 0

    def __call__(self, method, url, **kw):
        r = self.inner(method, url, **kw)
        if self.folder:
            self.n += 1
            path = re.sub(r"[0-9a-f-]{20,}", "ID", url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]).replace("/", "_")
            body = json.loads(adhttp.scrub(json.dumps(r.body), self.secrets)) if r.body is not None else None
            with open(os.path.join(self.folder, f"{self.n:02d}_{method}_{path}.json"), "w") as f:
                json.dump({"status": r.status, "body": body}, f, indent=1)
        return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--profile", required=True, help="the public profile id the ad shows")
    ap.add_argument("--video", required=True, help="a 9:16 MP4 under 32 MB")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--budget", type=float, default=5.0, help="daily budget in the account's currency (Snap's minimum 5)")
    ap.add_argument("--timezone", default="America/Los_Angeles")
    ap.add_argument("--objective", choices=adspec.OBJECTIVES, default="traffic")
    ap.add_argument("--pixel")
    ap.add_argument("--landing", default="https://campaignify.ai/")
    ap.add_argument("--fail-at", help="creative:1, campaign, adset or ad:1")
    ap.add_argument("--record")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    start = date.today() + timedelta(days=1)
    cr = adspec.CreativeSpec(kind="video", asset_id=1, name="Sandbox video", headline="Campaignify sandbox test",
                             cta="LEARN_MORE", destination_url=a.landing, utm=snapchat.UTM, aspect="9:16")
    spec = adspec.CampaignSpec(objective=a.objective, platform="snapchat", name=f"Campaignify sandbox {int(time.time())}",
                               daily_budget_minor=int(a.budget * adspec.minor_factor(a.currency)),
                               currency=a.currency, start_at=start.isoformat(), geo={"countries": ["US"]},
                               audience={"age_min": 18, "age_max": 65, "genders": ["all"]}, creatives=[cr],
                               conversion={"pixel_id": a.pixel, "event": "Purchase"} if a.pixel else None)
    row = {"external_account_id": a.account, "page_id": a.profile, "timezone": a.timezone}
    steps = snapchat.plan_steps(spec, row, {1: {"kind": "video", "path": a.video}})
    issues = [i for i in adspec.validate(spec) if i.level == "block"]
    if issues:
        sys.exit("The test spec doesn't validate: " + "; ".join(i.message for i in issues))
    if a.dry_run:
        print(json.dumps(steps, indent=1))
        print(f"DRY RUN: {len(steps)} steps")
        sys.exit(0)
    refresh = os.environ.get("SNAPCHAT_SANDBOX_REFRESH_TOKEN") or ""
    if not (refresh and snapchat.client_id() and snapchat.client_secret()):
        sys.exit("Set SNAPCHAT_SANDBOX_REFRESH_TOKEN, SNAPCHAT_CLIENT_ID and SNAPCHAT_CLIENT_SECRET.")
    db.init()                                 # adhttp's rate budget lives in a throwaway SQLite file
    if a.record:
        os.makedirs(a.record, exist_ok=True)
    adhttp.TRANSPORT = Recorder(adhttp._requests_transport, a.record, [refresh, snapchat.client_secret()])
    lid = int(time.time()) % 100000000
    ctx = {"launch_id": lid, "account": a.account, "token": refresh, "results": {}, "max_wait": 120,
           "currency": a.currency, "timezone": a.timezone}
    done, failed = [], None
    for s in steps:
        if s["key"] == a.fail_at:
            s["request"][s["kind"]]["type"] = "NOT_A_TYPE"
        for _ in range(80):
            try:
                ctx["results"][s["key"]] = r = snapchat.execute_step(s, dict(ctx, lookup=False))
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
    access = snapchat.access_token(refresh)
    ok = True
    if failed:
        s, e = failed
        print(f"  {s['key']} failed as asked: {getattr(e, 'code', '')} {getattr(e, 'user_message', '') or e}")
        ok = a.fail_at is not None
        for d in reversed(done):
            print(f"  compensate {d['key']}: {snapchat.compensate(d, ctx)}")
    else:
        for s in done:
            if s["kind"] in ("campaign", "adsquad", "ad"):
                obj = snapchat._read(s["kind"], s["external_id"], ctx, access)
                good = bool(obj) and obj.get("status") == "PAUSED"
                ok = ok and good
                print(f"  {s['key']}: {'there, PAUSED' if good else 'MISSING or not paused'}")
        camp = next(s for s in steps if s["key"] == "campaign")
        again = snapchat.execute_step(camp, dict(ctx, lookup=True))
        ok = ok and again.get("found") and again["id"] == camp["external_id"]
        print("  a second run found the campaign and made no second one" if again.get("found") else
              "  the second run made something new")
        rows = snapchat.pull_insights(ctx, [{"kind": "campaign", "external_id": camp["external_id"]}],
                                      (date.today() - timedelta(days=1), date.today()))
        print(f"  stats: {len(rows)} daily rows, spend {sum(x['spend_minor'] for x in rows)} minor units")
    if not a.keep:
        for s in reversed(done):
            if s["kind"] in ("ad", "adsquad", "campaign") and s.get("external_id"):
                path = {"ad": "ads", "adsquad": "adsquads", "campaign": "campaigns"}[s["kind"]]
                try:
                    snapchat._call("DELETE", f"/{path}/{s['external_id']}", access, account=a.account)
                    print(f"  deleted {s['key']}")
                except adhttp.AdHttpError as e:
                    print(f"  couldn't delete {s['key']}: {e}")
        print("  cleaned up (creatives and media stay in the account's library)")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

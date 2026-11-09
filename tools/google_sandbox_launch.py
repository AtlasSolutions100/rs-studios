"""The M3.2 / M3.3 launch on a real Google Ads test account: the acceptance run the fake endpoint
(tests/fakegoogleads.py) stands in for. Run it on the server or a laptop, never in CI; it needs no app database.

    export GOOGLE_ADS_CLIENT_ID=... GOOGLE_ADS_CLIENT_SECRET=...   # or GOOGLE_OAUTH_CLIENT_ID / _SECRET: the OAuth
                                                                  # client of the Cloud project with Google Ads API access
    export GOOGLE_ADS_SANDBOX_REFRESH_TOKEN=...                   # a refresh token with the adwords scope for a Google
                                                                  # login that can reach the test account (see below)
    python tools/google_sandbox_launch.py --customer 123-456-7890 [--login 999-888-7777] --type search \\
        [--landing https://campaignify.ai/] [--fail-at campaign|check] [--record tests/fixtures/google/recorded] [--keep]
    python tools/google_sandbox_launch.py --customer ... --type pmax --image ad.png --logo logo.png [--fail-at upload:2]
    python tools/google_sandbox_launch.py --type search --customer 1234567890 --dry-run       # no network: the calls

Getting the refresh token once: connect the test account in Campaignify (Settings > Ad accounts > Connect Google
Ads) on a server with GOOGLE_ADS=1, or use Google's OAuth Playground with your own OAuth client and the scope
https://www.googleapis.com/auth/adwords (Settings: "Use your own OAuth credentials", access type offline).

What it does, with the same adapter code the worker runs (adplatforms/google.py):
  1. a PAUSED Search campaign (budget, campaign, ad group, 3 keywords, a responsive search ad) or a PAUSED
     Performance Max campaign (images and logo uploaded, then budget, campaign with brand guidelines, one asset
     group) in ONE GoogleAdsService.Mutate, then the GAQL check;
  2. checks with GAQL that the campaign is there and PAUSED;
  3. runs the campaign step again with the lookup on: it must find the campaign and send no second mutate;
  4. with --fail-at <step>, breaks that step (a field Google refuses) and checks nothing was made (the mutate is
     atomic) and earlier uploads are kept;
  5. cleans up: removes the campaign and its budget (unless --keep); uploaded images stay in the asset library
     (Google has no delete for them);
  6. with --record, writes every answer Google gave (tokens scrubbed) as JSON, to compare with or replace the
     hand-written tests/fixtures/google/*.json.

Exit 0 when every check passed. Use a Google Ads TEST account: nothing there ever serves or spends, and Test
access (the default for a new Cloud project) reaches only test accounts anyway.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RS_DATA", os.path.join(os.environ.get("TMPDIR", tempfile.gettempdir()), "cfy_google_sandbox"))

import adhttp  # noqa: E402
import adspec  # noqa: E402
import db  # noqa: E402
from adplatforms import google  # noqa: E402


class Recorder:
    def __init__(self, inner, folder, secrets):
        self.inner, self.folder, self.secrets, self.n = inner, folder, secrets, 0

    def __call__(self, method, url, **kw):
        r = self.inner(method, url, **kw)
        if self.folder:
            self.n += 1
            path = re.sub(r"\d{6,}", "N", url.split("://", 1)[-1].split("/", 1)[-1]).replace("/", "_").replace(":", "-")
            body = json.loads(adhttp.scrub(json.dumps(r.body), self.secrets)) if r.body is not None else None
            with open(os.path.join(self.folder, f"{self.n:02d}_{method}_{path}.json"), "w") as f:
                json.dump({"status": r.status, "body": body}, f, indent=1)
        return r


def build_spec(a):
    name = f"Campaignify sandbox {int(time.time())}"
    base = dict(objective="traffic", platform="google", name=name, daily_budget_minor=500, currency=a.currency,
                geo={"countries": ["US"]}, audience={"age_min": 18, "age_max": 65, "genders": ["all"]})
    if a.type == "search":
        g = {"type": "search", "final_url": a.landing, "bidding": "clicks", "language": "en", "search_partners": False,
             "keywords": [{"term": "campaignify sandbox test", "match": "exact"},
                          {"term": "campaignify sandbox", "match": "phrase"}, {"term": "sandbox ad test", "match": "broad"}],
             "negative_keywords": [{"term": "jobs", "match": "phrase"}],
             "headlines": ["Campaignify Sandbox", "Paused Test Campaign", "Nothing Runs Here"], "headline_pins": [1, 0, 0],
             "descriptions": ["A paused test campaign made by the sandbox run.", "It never serves on a test account."],
             "description_pins": [0, 0], "path1": "sandbox"}
        return adspec.CampaignSpec(creatives=[], google=g, **base), {}
    if not (a.image and a.logo):
        sys.exit("Performance Max needs --image and --logo.")
    g = {"type": "pmax", "final_url": a.landing, "bidding": "conversions", "language": "en",
         "headlines": ["Campaignify Sandbox", "Paused Test Campaign", "Nothing Runs Here"],
         "long_headlines": ["A paused Performance Max test campaign made by the sandbox run"],
         "descriptions": ["It never serves on a test account.", "Made by tools/google_sandbox_launch.py to check the adapter."],
         "business_name": "Campaignify", "logos": [1]}
    kind = google.logo_kind(a.logo)
    if not kind:
        sys.exit("The logo is under 128 pixels.")
    cr = adspec.CreativeSpec(kind="image", asset_id=1, name="Sandbox image", destination_url=a.landing, aspect="1:1")
    assets = {1: {"kind": "image", "path": a.image}, "logo:1": {"path": a.logo, "field": kind[0], "padded": kind[1]}}
    return adspec.CampaignSpec(creatives=[cr], google=g, **base), assets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", required=True, help="the test account's customer id (dashes allowed)")
    ap.add_argument("--login", help="a manager's customer id, when the login reaches the account through it")
    ap.add_argument("--type", choices=["search", "pmax"], default="search")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--landing", default="https://campaignify.ai/")
    ap.add_argument("--image")
    ap.add_argument("--logo")
    ap.add_argument("--fail-at", help="campaign, check, or upload:<n> (Performance Max)")
    ap.add_argument("--record")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the planned calls and exit (no network)")
    a = ap.parse_args()
    customer, login = re.sub(r"\D", "", a.customer), re.sub(r"\D", "", a.login or "") or None
    spec, assets = build_spec(a)
    row = {"external_account_id": customer}
    steps = google.plan_steps(spec, row, assets)
    issues = [i for i in adspec.validate(spec) if i.level == "block"]
    if issues:
        sys.exit("The test spec doesn't validate: " + "; ".join(i.message for i in issues))
    if a.dry_run:
        print(json.dumps(steps, indent=1))
        print(f"DRY RUN: {len(steps)} steps, {sum(1 for s in steps if s['kind'] == 'upload')} uploads, 1 mutate")
        sys.exit(0)
    refresh = os.environ.get("GOOGLE_ADS_SANDBOX_REFRESH_TOKEN") or ""
    if not (refresh and google.client_id() and google.client_secret()):
        sys.exit("Set GOOGLE_ADS_SANDBOX_REFRESH_TOKEN and the OAuth client (GOOGLE_ADS_CLIENT_ID / _SECRET).")
    db.init()                                 # adhttp's rate budget lives in a throwaway SQLite file
    if a.record:
        os.makedirs(a.record, exist_ok=True)
    adhttp.TRANSPORT = Recorder(adhttp._requests_transport, a.record, [refresh, google.client_secret()])
    lid = int(time.time()) % 100000000
    ctx = {"launch_id": lid, "account": customer, "login": login, "token": refresh, "results": {}, "max_wait": 120}
    done, failed = [], None
    for s in steps:
        if s["key"] == a.fail_at:
            if s["kind"] == "campaign":
                s["request"]["operations"][1]["campaignOperation"]["create"]["advertisingChannelType"] = "NOT_A_TYPE"
            elif s["kind"] == "upload":
                s["request"]["path"] = __file__        # not an image: Google refuses it
            else:
                s["kind"] = "nope"
        for _ in range(30):
            try:
                ctx["results"][s["key"]] = r = google.execute_step(s, dict(ctx, lookup=False))
                break
            except adhttp.RateLimited as e:
                time.sleep(min(300, e.retry_after or 30))
            except (adhttp.AdHttpError, KeyError, OSError) as e:
                failed = (s, e)
                break
        if failed:
            break
        s["external_id"], s["result"] = r.get("id"), r
        done.append(s)
        print(f"  made {s['key']}: {r.get('resource_name') or r}")
    ok = True
    access = google.access_token(refresh)
    if failed:
        s, e = failed
        print(f"  {s['key']} failed as asked: {getattr(e, 'code', '')} {getattr(e, 'user_message', '') or e}")
        found = google._find_campaign(ctx, access)
        ok = a.fail_at is not None and (found is None or s["kind"] == "check")
        print("  nothing of the campaign was left half made" if found is None else
              f"  the campaign stays, PAUSED: {found.get('resourceName')} ({found.get('status')})")
        for d in reversed(done):
            print(f"  compensate {d['key']}: {google.compensate(d, ctx)}")
    else:
        found = google._find_campaign(ctx, access)
        good = found and str(found.get("status")) == "PAUSED" and found.get("resourceName") == ctx["results"]["campaign"]["resource_name"]
        ok = bool(good)
        print(f"  campaign: {'found, PAUSED' if good else 'MISSING or not paused'}")
        camp = next(s for s in steps if s["key"] == "campaign")
        again = google.execute_step(camp, dict(ctx, lookup=True))
        ok = ok and again.get("found") and again["id"] == camp["external_id"]
        print("  a second run found the campaign and sent no second mutate" if ok else "  the second run made something new")
    if found and not a.keep:
        rn = found.get("resourceName")
        budget = google.search(access, customer, f"SELECT campaign.campaign_budget FROM campaign WHERE campaign.resource_name = "
                               f"{google.gaql_str(rn)}", login=login)
        google._call("POST", f"/customers/{customer}/campaigns:mutate", access, login=login,
                     body={"operations": [{"remove": rn}]}, account=customer)
        b = ((budget or [{}])[0].get("campaign") or {}).get("campaignBudget")
        if b:
            google._call("POST", f"/customers/{customer}/campaignBudgets:mutate", access, login=login,
                         body={"operations": [{"remove": b}]}, account=customer)
        print("  cleaned up (the campaign and its budget removed; uploaded images stay in the asset library)")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

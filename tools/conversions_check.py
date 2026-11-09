"""The M3.4 acceptance run against the live APIs, with the code the app runs (conversions.ws_build + adhttp): one
test Lead to a Meta pixel's Conversions API, a TikTok pixel's Events API and Google's Data Manager API. Run it on
the server or a laptop, never in CI; it needs no app database.

    export META_CHECK_TOKEN=...          # a Conversions API token for the pixel (Events Manager > the dataset >
                                         # Settings > Generate access token), or the Meta connection's token to see
                                         # whether META_CAPI_USE_CONNECTION_TOKEN=1 can work
    export TIKTOK_CHECK_TOKEN=...        # the pixel's Events API access token
    export GOOGLE_CHECK_REFRESH_TOKEN=...   # a refresh token with the adwords and datamanager scopes (connect Google
                                            # Ads in Campaignify with CONVERSIONS_API=1, or Google's OAuth Playground)
    export GOOGLE_ADS_CLIENT_ID=... GOOGLE_ADS_CLIENT_SECRET=...  # or GOOGLE_OAUTH_CLIENT_ID / _SECRET

    python tools/conversions_check.py --meta-pixel 123456789012345 --meta-test-code TEST12345
    python tools/conversions_check.py --tiktok-pixel C4AB12CD34EF56GH78IJ --tiktok-test-code TEST12345
    python tools/conversions_check.py --google-customer 123-456-7890 --google-action 987654321 [--login 999-888-7777]
                                      [--google-send]       # default: validateOnly, Google checks but keeps nothing
    python tools/conversions_check.py ... --dry-run          # no network: print what would be sent
    python tools/conversions_check.py ... --record tests/fixtures/recorded   # keep the answers (tokens scrubbed)

What to look for:
  * Meta: "events_received": 1. In Events Manager > Test events (with --meta-test-code) a Lead from Server. Then
    set the same code on the pixel in Campaignify (Settings > Conversion tracking), send a form on a measured page,
    and check the Lead shows from Browser and Server, marked deduplicated.
  * TikTok: "code": 0, and the SubmitForm event in Test events.
  * Google: an answer with a requestId (validateOnly: Google accepted the shape, the destination and the scope).
    --google-action is the "Campaignify lead (server)" action's id (Settings > Conversion tracking shows it).
Exit 0 when every platform asked for answered OK.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adhttp  # noqa: E402
import conversions  # noqa: E402
import site_events  # noqa: E402

EMAIL, PHONE = "conversions-check@example.com", "+15125550100"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--meta-pixel")
    ap.add_argument("--meta-test-code")
    ap.add_argument("--tiktok-pixel")
    ap.add_argument("--tiktok-test-code")
    ap.add_argument("--google-customer")
    ap.add_argument("--google-action")
    ap.add_argument("--login")
    ap.add_argument("--google-send", action="store_true", help="really import the lead (default: validateOnly)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--record")
    a = ap.parse_args()
    eid = site_events.new_event_id()
    hashes = conversions.lead_hashes(EMAIL, PHONE)
    ctx = {"ip": "203.0.113.10", "ua": "Mozilla/5.0 (conversions_check)", "url": "https://campaignify.ai/p/check",
           "gclid": None}
    jobs = []
    if a.meta_pixel:
        jobs.append(("meta", {"external_pixel_id": a.meta_pixel, "test_code": a.meta_test_code},
                     os.environ.get("META_CHECK_TOKEN", "")))
    if a.tiktok_pixel:
        jobs.append(("tiktok", {"external_pixel_id": a.tiktok_pixel.upper(), "test_code": a.tiktok_test_code},
                     os.environ.get("TIKTOK_CHECK_TOKEN", "")))
    if a.google_customer:
        if not a.google_action:
            ap.error("--google-action is required with --google-customer")
        if not a.google_send:
            os.environ["GOOGLE_DATAMANAGER_VALIDATE_ONLY"] = "1"
        jobs.append(("google", {"customer_id": re.sub(r"\D", "", a.google_customer), "external_id": a.google_action,
                                "login_id": a.login}, os.environ.get("GOOGLE_CHECK_REFRESH_TOKEN", "")))
    if not jobs:
        ap.error("name at least one of --meta-pixel, --tiktok-pixel, --google-customer")
    ok_all = True
    for platform, dest, secret in jobs:
        payload = {"event": "Lead", "event_id": eid, "ts": int(time.time()), "ctx": ctx, "user": hashes.get(platform, {})}
        if a.dry_run:
            req = conversions.ws_build(platform, payload, dest, "<token>")
            print(f"== {platform} (dry run)\nPOST {req['url']}\n{json.dumps(req['json'], indent=1)}")
            continue
        if not secret:
            print(f"== {platform}: no token in the environment, skipped")
            ok_all = False
            continue
        try:
            if platform == "google":
                from adplatforms import google
                secret = google.access_token(secret)
            req = conversions.ws_build(platform, payload, dest, secret)
            body = adhttp.call(platform, "POST", req["url"], params=req["params"], headers=req["headers"],
                               json_body=req["json"], retries=1, secrets=[secret])
            ok = not (platform == "meta" and int(body.get("events_received") or 0) < 1)
            print(f"== {platform}: {'OK' if ok else 'NOT OK'}\n{json.dumps(body, indent=1)}")
        except adhttp.AdHttpError as e:
            ok, body = False, {"error": str(e)}
            print(f"== {platform}: FAILED ({conversions._kind(e)})\n{e}")
        ok_all = ok_all and ok
        if a.record:
            os.makedirs(a.record, exist_ok=True)
            with open(os.path.join(a.record, f"{platform}_lead_{'ok' if ok else 'error'}.json"), "w") as f:
                json.dump(json.loads(adhttp.scrub(json.dumps(body), [secret])), f, indent=1)
    print(f"\nevent id {eid}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())

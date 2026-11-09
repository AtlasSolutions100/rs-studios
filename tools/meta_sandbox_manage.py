"""The M2.4 manage calls on a real Meta ad account (a sandbox or test account): the acceptance run the fake Graph stands
in for. Run it on the server or a laptop, never in CI; it needs no database.

    export META_APP_ID=... META_APP_SECRET=... META_SANDBOX_TOKEN=...       # as for tools/meta_sandbox_launch.py
    python tools/meta_sandbox_launch.py --account act_123 --page 456 --image ad.png --video ad.mp4 --keep
    python tools/meta_sandbox_manage.py --account act_123 --campaign <id> --adset <id> --ad <id> \\
        [--record tests/fixtures/meta/recorded_manage]

What it does, with the same adapter code the app runs (adplatforms/meta.py):
  1. reads the campaign, ad set and ad in two multi-id reads (budgeted objects, then ads) and checks they are PAUSED;
  2. raises the campaign's daily budget by 1.00, reads it back, waits out the 30 s edit slot, puts it back;
  3. turns the ad on and off again while the campaign stays PAUSED (nothing can deliver or spend), reading it back
     each time (effective_status CAMPAIGN_PAUSED or ADSET_PAUSED while on);
  4. sends a second change inside 30 s: our edit lock (adhttp.edit_lock) holds it before Meta is called;
  5. with --record, writes every answer Meta gave (tokens and ids scrubbed) as JSON fixtures, to replace the
     hand-written tests/fixtures/meta/manage_*.json.

It never turns the campaign on. Exit 0 when every check passed.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("RS_DATA", os.path.join(os.environ.get("TMPDIR", "/tmp"), "cfy_sandbox_manage"))

import adhttp  # noqa: E402
import db  # noqa: E402
from adplatforms import meta  # noqa: E402
from meta_sandbox_launch import Recorder  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--adset", required=True)
    ap.add_argument("--ad", required=True)
    ap.add_argument("--record")
    a = ap.parse_args()
    token = os.environ.get("META_SANDBOX_TOKEN") or ""
    if not (token and meta.app_id() and meta.app_secret()):
        sys.exit("Set META_SANDBOX_TOKEN, META_APP_ID and META_APP_SECRET.")
    db.init()                                 # adhttp's rate budget and edit locks live in a throwaway SQLite file
    if a.record:
        os.makedirs(a.record, exist_ok=True)
    adhttp.TRANSPORT = Recorder(adhttp._requests_transport, a.record, [token, meta.app_secret()])
    acct = a.account.replace("act_", "")
    ids = {"campaign": [a.campaign], "adset": [a.adset], "ad": [a.ad]}
    ok = True

    def say(good, what):
        nonlocal ok
        ok = ok and bool(good)
        print(f"  {'ok ' if good else 'BAD'} {what}")

    st = meta.read_objects(token, acct, ids)
    say(all(st.get(i, {}).get("status") == "PAUSED" for i in (a.campaign, a.adset, a.ad)), "all three read, all PAUSED")
    budget = st.get(a.campaign, {}).get("daily_budget")
    say(budget, f"the campaign has a daily budget ({budget} minor units)")
    if not budget:
        print("FAIL")
        sys.exit(1)
    meta.update_budget(token, acct, a.campaign, budget + 100, max_wait=40)
    say(meta.read_objects(token, acct, {"campaign": [a.campaign]})[a.campaign]["daily_budget"] == budget + 100,
        "the budget change reads back")
    try:                                       # inside the 30 s slot: our lock refuses before Meta is called
        meta.update_budget(token, acct, a.campaign, budget, max_wait=0)
        say(False, "a second change inside 30 s is held")
    except adhttp.RateLimited as e:
        say(e.retry_after > 0, f"a second change inside 30 s is held ({int(e.retry_after)} s left)")
    meta.update_budget(token, acct, a.campaign, budget, max_wait=40)
    say(meta.read_objects(token, acct, {"campaign": [a.campaign]})[a.campaign]["daily_budget"] == budget,
        "and the budget is back")
    meta.set_status(token, acct, a.ad, "ACTIVE", max_wait=40)
    s = meta.read_objects(token, acct, {"ad": [a.ad]})[a.ad]
    say(s["status"] == "ACTIVE" and s["effective_status"] in ("CAMPAIGN_PAUSED", "ADSET_PAUSED"),
        f"the ad is on and waits for its paused parent ({s['effective_status']})")
    meta.set_status(token, acct, a.ad, "PAUSED", max_wait=40)
    say(meta.read_objects(token, acct, {"ad": [a.ad]})[a.ad]["status"] == "PAUSED", "and paused again")
    st = meta.read_objects(token, acct, {"campaign": [a.campaign]})
    say(st[a.campaign]["status"] == "PAUSED", "the campaign stayed PAUSED throughout")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

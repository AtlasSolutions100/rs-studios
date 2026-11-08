"""The out-of-credits paywall (RS_PLAN_PAYWALL, default off).

A workspace with no subscription (plans.plan_code == "free") that is out of
credits (less than one image ad left) can still make one campaign plan with
competitor research every 30 days, free: we absorb what the AI calls cost
(planner.run(free=True), competitors.run with inputs.free). The plan page
shows part of it (the summary, the first channel's recommendation and the top
3 competitor insights); the rest sits behind a lock with "Get an active
subscription to unlock", linking to the plans. Anything else that costs
credits shows the same lock and message (core.reserve_ai and core.queue_job
raise PaywallLocked; every app page shows the lock banner). A subscription
(any paid plan, a comp or a free-month code) unlocks everything already made.

Who gets the free plan (M2 re-audit M-1, L-5, L-6, L-7):
  * one per person, not per workspace: the 30 days are counted per workspace,
    per card fingerprint and per normalized email (free_plans.fingerprint,
    free_plans.email_norm), so N throwaway accounts on one card get one plan;
  * the workspace needs a saved card that earned the starter credits
    (card_trials.trial=1), or else a member with a confirmed email. A card or
    email that already had a trial on another account gets no free plan either;
  * credits still in flight (open credit holds, queued or running jobs) count
    as balance for the claim, so a pack holder can't dip under one image ad by
    starting something expensive and claim it;
  * a run that fails gives the plan back, at most MAX_RETRIES times per 30 days
    (failed rows stay, with status 'failed').
"""
import json
import os
from datetime import datetime, timedelta, timezone

from werkzeug.exceptions import HTTPException

import card_on_file
import catalog
import db
import plans
import preview
import verify

FREE_PLAN_DAYS = 30
MAX_RETRIES = 2           # failed free plans given back per 30 days (L-7): the first try plus 2 more
FREE_COMPETITORS = 3      # the most competitors a free plan's research covers (it finds them)
TOP_INSIGHTS = 3
MESSAGE = "Get an active subscription to unlock"


def on() -> bool:
    """RS_PLAN_PAYWALL: 1 / on turns it on (default off)."""
    return (os.environ.get("RS_PLAN_PAYWALL") or "").strip().lower() in ("1", "on", "true", "yes")


def subscribed(ws) -> bool:
    """A paid plan, an admin comp or a free-month code."""
    return ws is not None and plans.plan_code(ws) != "free"


def min_price() -> int:
    return catalog.price_credits("ad_image", batch=1)


def out_of_credits(ws) -> bool:
    return ws is not None and db.balance(ws) < min_price()


def applies(ws) -> bool:
    """This workspace sees the lock: the paywall is on, no subscription, out of credits."""
    return on() and ws is not None and not subscribed(ws) and out_of_credits(ws)


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def in_flight(conn, ws_id: int) -> int:
    """Credits taken but maybe coming back: open AI holds plus queued and running jobs (L-6)."""
    held = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM credit_holds WHERE workspace_id=? AND status='held'",
                        (ws_id,)).fetchone()[0]
    jobs = conn.execute("SELECT COALESCE(SUM(credits), 0) FROM jobs WHERE workspace_id=? AND status IN "
                        "('pending', 'running')", (ws_id,)).fetchone()[0]
    return int(held or 0) + int(jobs or 0)


def claimable(conn, ws) -> bool:
    """The paywall applies and stays applied with every in-flight spend counted back."""
    return applies(ws) and db.balance(ws) + in_flight(conn, ws["id"]) < min_price()


def card_row(conn, ws_id: int):
    """The workspace's saved card (card_trials), newest first, or None."""
    return conn.execute("SELECT * FROM card_trials WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (ws_id,)).fetchone()


def who(conn, ws, user) -> dict:
    """The keys a free plan is counted on: the workspace, its card and the member's normalized email."""
    card = card_row(conn, ws["id"]) if ws is not None else None
    email = preview.norm_email(user["email"] if user is not None else "") or None
    return {"ws_id": ws["id"] if ws is not None else 0, "fingerprint": card["fingerprint"] if card is not None else None,
            "email_norm": email if email and "@" in email else None}


_MATCH = ("(workspace_id=? OR (fingerprint IS NOT NULL AND fingerprint=?) OR (email_norm IS NOT NULL AND email_norm=?))"
          " AND created_at>?")


def _args(k: dict) -> tuple:
    return (k["ws_id"], k["fingerprint"] or "", k["email_norm"] or "", _since(FREE_PLAN_DAYS))


def refusal(conn, ws, user) -> str | None:
    """Why this member can't have a free plan at all: "reused" (the card or email had a trial on another
    account) or "confirm" (no card that earned the trial and no confirmed email). None when they can."""
    if ws is None or user is None:
        return "confirm"
    card = card_row(conn, ws["id"])
    if card is not None:
        return None if card["trial"] else "reused"
    if not verify.is_verified(user):
        return "confirm"
    if card_on_file.trial_used(conn, ws["id"], None, user["email"]):
        return "reused"
    return None


def last_free(conn, k: dict):
    """The free plan (made or running) on any of these keys in the last 30 days, or None."""
    return conn.execute(f"SELECT * FROM free_plans WHERE {_MATCH} AND COALESCE(status, '') != 'failed' "
                        "ORDER BY id DESC LIMIT 1", _args(k)).fetchone()


def failed(conn, k: dict) -> list:
    """Failed free plans on any of these keys in the last 30 days, oldest first."""
    return conn.execute(f"SELECT * FROM free_plans WHERE {_MATCH} AND status='failed' ORDER BY created_at",
                        _args(k)).fetchall()


def next_free_on(conn, k: dict) -> str | None:
    """The date (YYYY-MM-DD) the next free plan can be made, or None when one can be made now."""
    row = last_free(conn, k)
    if row is None:
        bad = failed(conn, k)
        if len(bad) <= MAX_RETRIES:
            return None
        row = bad[len(bad) - MAX_RETRIES - 1]      # once this one is 30 days old, one more try opens
    made = datetime.fromisoformat(row["created_at"])
    return (made + timedelta(days=FREE_PLAN_DAYS)).date().isoformat()


def available(conn, ws, user) -> bool:
    return claimable(conn, ws) and refusal(conn, ws, user) is None and next_free_on(conn, who(conn, ws, user)) is None


def claim(conn, k: dict) -> int | None:
    """Take the free plan for these keys for the next 30 days, atomically (no commit). Returns the
    free_plans id, or None when one was made (or is running) in the window on the workspace, the card or
    the email, or the failed tries are used up."""
    a = _args(k)
    cur = conn.execute("INSERT INTO free_plans (workspace_id, fingerprint, email_norm, status, created_at) "
                       f"SELECT ?, ?, ?, 'running', ? WHERE NOT EXISTS (SELECT 1 FROM free_plans WHERE {_MATCH} "
                       "AND COALESCE(status, '') != 'failed') "
                       f"AND (SELECT COUNT(*) FROM free_plans WHERE {_MATCH} AND status='failed') <= ?",
                       (k["ws_id"], k["fingerprint"], k["email_norm"], db.now(), *a, *a, MAX_RETRIES))
    return cur.lastrowid if cur.rowcount == 1 else None


def done(claim_id: int):
    """The free plan was made."""
    with db.connect() as conn:
        conn.execute("UPDATE free_plans SET status='ready' WHERE id=?", (claim_id,))
        conn.commit()


def give_back(claim_id: int):
    """The free plan produced nothing: it no longer counts, but the failure does (MAX_RETRIES)."""
    with db.connect() as conn:
        conn.execute("UPDATE free_plans SET status='failed' WHERE id=?", (claim_id,))
        conn.commit()


GENERIC_STEPS = {"read": "Read the product and the template catalog", "angles": "Planned the angles",
                 "creative": "Wrote hooks and scripts", "ad": "Lined up the ads", "copy": "Wrote the platform copy",
                 "seo": "Wrote the search keywords", "done": "Finished the plan", "error": "Stopped with an error"}


def generic_steps(steps: list) -> list:
    """The planning steps of a free plan with nothing locked in them (L-8): no angle names, hooks or the
    model's own words, only what kind of step it was."""
    return [{"kind": s.get("kind", "note"), "title": GENERIC_STEPS.get(s.get("kind"), "Planning the campaign")}
            for s in steps or [] if isinstance(s, dict)]


def locked(c, ws) -> bool:
    """Show only part of this campaign: it was made free and the workspace has no subscription."""
    return bool(c is not None and "free_plan" in c.keys() and c["free_plan"]) and not subscribed(ws)


def _insight_text(key: str, it: dict) -> str:
    if key == "angles":
        return f"{it.get('angle', '')}: {it.get('hook', '')}".strip(": ")
    if key == "objections":
        return it.get("objection", "")
    return it.get("text", "")


def insights(report: dict) -> list[dict]:
    """Every competitor insight of a report, best first: positioning gaps, angles, objections."""
    s = (report or {}).get("synthesis") or {}
    out = []
    for key in ("gaps", "angles", "objections"):
        for it in s.get(key) or []:
            t = " ".join(str(_insight_text(key, it) or "").split())
            if t:
                out.append({"kind": key, "text": t[:300], "sources": list(it.get("sources") or [])[:2]})
    return out


def partial(plan: dict, report: dict | None) -> dict:
    """What the locked plan page shows, and what it holds back (for the lock rows)."""
    final = plan.get("final") or {}
    split = final.get("budget_split") or []
    copy = plan.get("copy") or []
    channel = None
    if split:
        channel = {"platform": split[0].get("platform", ""), "share": split[0].get("share_percent"),
                   "note": split[0].get("note", "")}
    elif copy:
        channel = {"platform": copy[0].get("platform", ""), "share": None, "note": ""}
    all_insights = insights(report)
    summary = final.get("summary") or ""
    held = [("Angles", len(plan.get("angles") or [])), ("Hooks and scripts", len(plan.get("creative") or [])),
            ("Platform copy", len(copy)), ("Channels and budget split", max(len(split) - 1, 0)),
            ("KPIs and test plan", len(final.get("kpis") or []) + (1 if final.get("test_plan") else 0)),
            ("Launch checklist", len(final.get("checklist") or [])),
            ("More competitor insights", max(len(all_insights) - TOP_INSIGHTS, 0))]
    if final.get("offer"):
        held.insert(0, ("The offer", 1))
    return {"summary": summary, "channel": channel, "insights": all_insights[:TOP_INSIGHTS],
            "held": [(label, n) for label, n in held if n]}


def report_for(conn, c) -> dict | None:
    """The competitor research a free plan was made with (its report, else the brand's latest)."""
    row = conn.execute("SELECT report_id FROM free_plans WHERE campaign_id=?", (c["id"],)).fetchone()
    rep = None
    if row and row["report_id"]:
        rep = conn.execute("SELECT * FROM competitor_reports WHERE id=? AND workspace_id=? AND status='ready'",
                           (row["report_id"], c["workspace_id"])).fetchone()
    if rep is None and c["brand_id"]:
        rep = conn.execute("SELECT * FROM competitor_reports WHERE workspace_id=? AND brand_id=? AND status='ready' "
                           "ORDER BY id DESC LIMIT 1", (c["workspace_id"], c["brand_id"])).fetchone()
    if rep is None:
        return None
    try:
        return json.loads(rep["report_json"] or "{}")
    except ValueError:
        return None


def report_locked(conn, r, ws) -> bool:
    """A competitor report made for a free plan, seen without a subscription."""
    if r is None or subscribed(ws):
        return False
    try:
        return bool(json.loads(r["inputs_json"] or "{}").get("free"))
    except ValueError:
        return False


class PaywallLocked(HTTPException):
    """Something that costs credits, on a workspace the paywall applies to (402)."""
    code = 402
    description = MESSAGE


def check(ws):
    """Raise PaywallLocked when this workspace sees the lock (called where a spend is refused)."""
    if applies(ws):
        raise PaywallLocked()

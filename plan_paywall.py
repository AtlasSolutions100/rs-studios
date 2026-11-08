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
"""
import json
import os
from datetime import datetime, timedelta, timezone

from werkzeug.exceptions import HTTPException

import catalog
import db
import plans

FREE_PLAN_DAYS = 30
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


def last_free(conn, ws_id: int):
    """The free plan this workspace made in the last 30 days, or None."""
    return conn.execute("SELECT * FROM free_plans WHERE workspace_id=? AND created_at>? ORDER BY id DESC LIMIT 1",
                        (ws_id, _since(FREE_PLAN_DAYS))).fetchone()


def next_free_on(conn, ws_id: int) -> str | None:
    """The date (YYYY-MM-DD) the next free plan can be made, or None when one can be made now."""
    row = last_free(conn, ws_id)
    if row is None:
        return None
    made = datetime.fromisoformat(row["created_at"])
    return (made + timedelta(days=FREE_PLAN_DAYS)).date().isoformat()


def available(conn, ws) -> bool:
    return applies(ws) and last_free(conn, ws["id"]) is None


def claim(conn, ws_id: int) -> int | None:
    """Take this workspace's free plan for the next 30 days, atomically (no commit).
    Returns the free_plans id, or None when one was already made in the window."""
    cur = conn.execute("INSERT INTO free_plans (workspace_id, created_at) SELECT ?, ? WHERE NOT EXISTS "
                       "(SELECT 1 FROM free_plans WHERE workspace_id=? AND created_at>?)",
                       (ws_id, db.now(), ws_id, _since(FREE_PLAN_DAYS)))
    return cur.lastrowid if cur.rowcount == 1 else None


def give_back(claim_id: int):
    """The free plan produced nothing: the workspace may try again."""
    with db.connect() as conn:
        conn.execute("DELETE FROM free_plans WHERE id=?", (claim_id,))
        conn.commit()


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

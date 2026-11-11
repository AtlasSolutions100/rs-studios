"""Plans only (PLANS_ONLY, default off): no free tier (Koby, Nov 11 2026).

While it is on:

  * A new sign-up gets no starter credits and picks a plan before anything else
    (plan_gate_routes.py: the "Pick a plan" page, then Stripe Checkout). Its card
    is saved by that checkout, so the separate card step (card_on_file.py) is not
    asked on top.
  * Starter and Growth start with a 3-day trial (TRIAL_DAYS, TRIAL_PLANS). Stripe
    runs the trial: the card is taken at checkout and charged the plan's price
    when the trial ends, unless the member cancels first. TRIAL_PERCENT of the
    plan's monthly credits land when the trial starts (200 on Starter, 600 on
    Growth); the rest land with the first payment, and each renewal after that
    is the full month. Scale has no trial and is charged at once.
  * One trial per workspace and per person (plan_trials: the workspace, and the
    owner's email with case, +tags and Gmail dots folded, preview.norm_email).
    A workspace that already paid for a plan, or is on a comp or a code, gets no
    trial either: it goes straight to the paid plan.
  * Accounts that were on Free pick a plan at their next visit to make anything
    new. Their library, downloads, sharing, Billing, the look switch, workspace
    deletion and logging out stay open (plan_gate_routes.ALLOWED).
  * Credit packs top up a plan; they are not sold without one.

Never asked for a plan: site admins; a workspace on a plan, an admin comp or a
free-month code; a checkout still confirming its payment; a subscription whose
payment failed (that is fixed on Billing, and the renewal hold already stops
spending); and a workspace that paid for a credit pack and still holds credits
(nothing that was paid for is taken away).

Switching the flag off puts everything back: sign-ups get starter credits again
and nobody is asked for a plan. Trials already running carry on in Stripe.
"""
import os
from datetime import datetime, timezone

from werkzeug.exceptions import HTTPException

import db
import plans
import preview

TRIAL_DAYS = 3
TRIAL_PLANS = ("starter", "growth")
TRIAL_PERCENT = 10          # of the plan's monthly credits, granted when the trial starts
MESSAGE = "Pick a plan to make something new."


class PlanRequired(HTTPException):
    """Something that makes or spends, on a workspace with no plan while PLANS_ONLY is on (402)."""
    code = 402
    description = MESSAGE


def on() -> bool:
    """PLANS_ONLY: 1 / on turns it on (default off)."""
    return (os.environ.get("PLANS_ONLY") or "").strip().lower() in ("1", "on", "true", "yes")


def _key(row, k):
    return row[k] if row is not None and k in row.keys() else None


def trial_credits(p) -> int:
    """The credits a trial of plan `p` starts with."""
    return int(p["credits"]) * TRIAL_PERCENT // 100


def offers_trial(code: str) -> bool:
    """Does this plan start with a trial for someone who hasn't had one? (For page copy.)"""
    return on() and code in TRIAL_PLANS


def _email_norm(user) -> str | None:
    e = preview.norm_email(_key(user, "email") or "") if user is not None else ""
    return e if e and "@" in e else None


def had_trial(conn, ws, user=None) -> bool:
    """This workspace or this person already had a trial, or the workspace already paid for a plan."""
    if ws is None:
        return True
    if conn.execute("SELECT 1 FROM plan_trials WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone():
        return True
    email = _email_norm(user)
    if email and conn.execute("SELECT 1 FROM plan_trials WHERE email_norm=? LIMIT 1", (email,)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM plan_invoices WHERE workspace_id=? AND amount_cents>0 LIMIT 1",
                        (ws["id"],)).fetchone() is not None


def trial_days(conn, ws, user, code: str) -> int:
    """The trial a plan checkout for `code` starts with: TRIAL_DAYS, or 0 (Scale, a second trial, a workspace
    already on a plan, comp or code, or the flag off)."""
    if not offers_trial(code) or ws is None or plans.plan_code(ws) != "free":
        return 0
    return 0 if had_trial(conn, ws, user) else TRIAL_DAYS


def exempt(conn, ws, user=None) -> bool:
    """A workspace that is never sent to pick a plan."""
    if ws is None:
        return True
    if user is not None and _key(user, "is_admin"):
        return True
    if plans.plan_code(ws) != "free":              # a plan (its trial too), an admin comp, a free-month code
        return True
    status = _key(ws, "plan_status")
    if status == "pending":                        # a plan checkout is confirming its payment
        return True
    if _key(ws, "stripe_subscription_id") and status in plans.LIVE_STATUSES | plans.RENEWAL_FAILED:
        return True                                # the payment failed: fixed on Billing; the renewal hold stops spending
    # Paid for credits before this was switched on and still holds some: they stay spendable.
    if int(_key(ws, "credits") or 0) > 0 and conn.execute(
            "SELECT 1 FROM purchases WHERE workspace_id=? AND status='paid' AND amount_cents>0 LIMIT 1",
            (ws["id"],)).fetchone() is not None:
        return True
    return False


def needs_plan(conn, ws, user=None) -> bool:
    return on() and not exempt(conn, ws, user)


def check(ws, user=None):
    """Raise PlanRequired where a spend would start on a workspace that has to pick a plan first."""
    if not on() or ws is None:
        return
    with db.connect() as conn:
        if needs_plan(conn, ws, user):
            raise PlanRequired()


def packs_open(ws, user=None) -> bool:
    """May this workspace buy a credit pack? Always, unless PLANS_ONLY is on and it has no plan."""
    if not on() or ws is None:
        return True
    return bool(user is not None and _key(user, "is_admin")) or plans.plan_code(ws) != "free"


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return None


def start_trial(conn, ws, sub_id: str, code: str, trial_end=None, user_id=None) -> int:
    """A trial subscription of ours began (its $0 invoice, or the subscription turning up as trialing; whichever
    Stripe sends first). Records it once per subscription and grants the trial credits. No commit. Returns the
    credits granted: 0 when this subscription's trial was recorded before, or when the workspace or the person
    already had a trial (the Stripe trial still runs; it just carries no credits of ours)."""
    p = plans.PLANS.get(code)
    if ws is None or not sub_id or p is None or code == "free":
        return 0
    if conn.execute("SELECT 1 FROM plan_trials WHERE subscription_id=?", (sub_id,)).fetchone():
        return 0
    owner = None
    if user_id and str(user_id).isdigit():
        owner = conn.execute("SELECT * FROM users WHERE id=? AND workspace_id=?", (int(user_id), ws["id"])).fetchone()
    if owner is None:
        owner = conn.execute("SELECT * FROM users WHERE workspace_id=? ORDER BY (role='owner') DESC, id LIMIT 1",
                             (ws["id"],)).fetchone()
    email = _email_norm(owner)
    again = conn.execute("SELECT 1 FROM plan_trials WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone() is not None \
        or bool(email and conn.execute("SELECT 1 FROM plan_trials WHERE email_norm=? LIMIT 1", (email,)).fetchone())
    # Word of the trial reached us after its first payment did (events out of order): record it, change nothing.
    paid = conn.execute("SELECT 1 FROM plan_invoices WHERE workspace_id=? AND amount_cents>0 LIMIT 1",
                        (ws["id"],)).fetchone() is not None
    n = 0 if (again or paid) else trial_credits(p)
    ends = _iso(trial_end)
    cur = conn.execute("INSERT OR IGNORE INTO plan_trials (workspace_id, user_id, email_norm, plan, subscription_id, "
                       "credits, started_at, ends_at) VALUES (?,?,?,?,?,?,?,?)",
                       (ws["id"], owner["id"] if owner is not None else None, email, code, sub_id, n, db.now(), ends))
    if cur.rowcount != 1 or paid:
        return 0
    if n:
        db.set_plan_credits(conn, ws["id"], n, f"{p['name']} plan: trial credits")
    conn.execute("UPDATE workspaces SET trial_open=1, trial_ends_at=COALESCE(?, trial_ends_at), plan_granted=?, "
                 "plan_paid_through=COALESCE(?, plan_paid_through) WHERE id=?", (ends, n, ends, ws["id"]))
    return n


def in_trial(ws) -> bool:
    """The workspace's subscription is in its trial (nothing has been paid yet)."""
    return ws is not None and _key(ws, "plan_status") == "trialing" and bool(_key(ws, "trial_open"))


def trial_view(ws) -> dict | None:
    """What Billing says about a running trial: the plan, when it ends, what is charged then, and the credits
    still to come with that first payment. None when no trial is running."""
    if not in_trial(ws):
        return None
    p = plans.PLANS.get(_key(ws, "plan") or "")
    if p is None or p["code"] == "free":
        return None
    had = int(_key(ws, "plan_granted") or 0)
    return {"plan": p, "ends_at": _key(ws, "trial_ends_at"), "usd": p["usd"], "credits_now": had,
            "credits_later": max(0, int(p["credits"]) - had),
            "canceling": bool(_key(ws, "plan_cancel_at_period_end"))}


def terms(code: str, days: int) -> dict:
    """What picking plan `code` means in money, for the plan page and the checkout note."""
    p = plans.PLANS[code]
    n = trial_credits(p) if days else int(p["credits"])
    return {"code": code, "name": p["name"], "usd": p["usd"], "days": days, "credits": int(p["credits"]),
            "credits_now": n, "credits_later": int(p["credits"]) - n if days else 0}


def checkout_note(code: str, days: int) -> str:
    """The line under the button on Stripe's page (custom_text.submit.message)."""
    t = terms(code, days)
    if days:
        return (f"{days} days free with {t['credits_now']:,} credits. Then ${t['usd']} a month with "
                f"{t['credits']:,} credits a month, until you cancel. Cancel any time in Billing; "
                f"cancel before the trial ends and nothing is charged.")
    return f"${t['usd']} a month with {t['credits']:,} credits a month, until you cancel. Cancel any time in Billing."

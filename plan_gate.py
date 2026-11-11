"""Plans only (PLANS_ONLY, default off): no free tier (Koby, Nov 11 2026).

While it is on:

  * A new sign-up gets no starter credits and picks a plan before anything else
    (plan_gate_routes.py: the "Pick a plan" page, then Stripe Checkout). Its card
    is saved by that checkout, so the separate card step (card_on_file.py) is not
    asked on top.
  * Starter and Growth start with a 3-day trial (TRIAL_DAYS, TRIAL_PLANS) with full access (Koby, Nov 11): the
    whole plan and its month of credits from the first minute (TRIAL_PERCENT = 100: 2,000 on Starter, 6,000 on
    Growth). Stripe runs the trial: the card is taken at checkout and charged the plan's price when the trial
    ends, unless the member cancels first. The first payment does not reset the credits: they are the first
    month's, and the member carries on with what is left; the next full month lands at the renewal after
    that. Scale has no trial and is charged at once.
  * One trial per workspace, per person and per card (plan_trials: the workspace, the owner's email with case,
    +tags and Gmail dots folded, preview.norm_email, and the card's fingerprint read from Stripe when the trial
    is first recorded; cards that had the old free credits count too, card_trials). A workspace that already
    paid for a plan, or is on a comp or a code, gets no trial either: it goes straight to the paid plan. A
    repeat that slips through to a Stripe trial (the card is only known after checkout) runs without trial
    credits; its credits land with the first payment.
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
TRIAL_PERCENT = 100         # of the plan's monthly credits, granted when the trial starts: full access
MESSAGE = "Pick a plan to make something new."


class PlanRequired(HTTPException):
    """Something that makes or spends, on a workspace with no plan while PLANS_ONLY is on (402)."""
    code = 402
    description = MESSAGE


def on() -> bool:
    """PLANS_ONLY: 1 / on turns it on (default off). It takes effect only while Stripe can run a plan checkout
    (billing.configured): without one nobody could pick a plan, so the app stays as it was."""
    if (os.environ.get("PLANS_ONLY") or "").strip().lower() not in ("1", "on", "true", "yes"):
        return False
    import billing
    return billing.configured()


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


def card_had_trial(conn, ws_id: int, fingerprint: str | None) -> bool:
    """Did this card start a trial (or earn the old free credits, card_on_file.py) on another workspace?"""
    if not fingerprint:
        return False
    if conn.execute("SELECT 1 FROM plan_trials WHERE fingerprint=? AND workspace_id!=? LIMIT 1",
                    (fingerprint, ws_id)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM card_trials WHERE fingerprint=? AND workspace_id!=? AND trial=1 LIMIT 1",
                        (fingerprint, ws_id)).fetchone() is not None


def start_trial(conn, ws, sub_id: str, code: str, trial_end=None, user_id=None, fingerprint: str | None = None) -> int:
    """A trial subscription of ours began (its $0 invoice, or the subscription turning up as trialing; whichever
    Stripe sends first). Records it once per subscription and grants the trial credits. No commit. Returns the
    credits granted: 0 when this subscription's trial was recorded before, or when the workspace, the person or
    the card (`fingerprint`) already had a trial (the Stripe trial still runs; it just carries no credits of
    ours, and they land with the first payment)."""
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
        or bool(email and conn.execute("SELECT 1 FROM plan_trials WHERE email_norm=? LIMIT 1", (email,)).fetchone()) \
        or card_had_trial(conn, ws["id"], fingerprint)
    # Word of the trial reached us after its first payment did (events out of order): record it, change nothing.
    paid = conn.execute("SELECT 1 FROM plan_invoices WHERE workspace_id=? AND amount_cents>0 LIMIT 1",
                        (ws["id"],)).fetchone() is not None
    n = 0 if (again or paid) else trial_credits(p)
    ends = _iso(trial_end)
    cur = conn.execute("INSERT OR IGNORE INTO plan_trials (workspace_id, user_id, email_norm, plan, subscription_id, "
                       "credits, started_at, ends_at, fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
                       (ws["id"], owner["id"] if owner is not None else None, email, code, sub_id, n, db.now(), ends,
                        fingerprint or None))
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
    """What Billing says about a running trial: the plan, when it ends and what is charged then. `no_credits`
    marks a repeat trial that came without credits. None when no trial is running."""
    if not in_trial(ws):
        return None
    p = plans.PLANS.get(_key(ws, "plan") or "")
    if p is None or p["code"] == "free":
        return None
    return {"plan": p, "ends_at": _key(ws, "trial_ends_at"), "usd": p["usd"],
            "no_credits": int(_key(ws, "plan_granted") or 0) == 0,
            "canceling": bool(_key(ws, "plan_cancel_at_period_end"))}


def terms(code: str, days: int) -> dict:
    """What picking plan `code` means in money, for the plan page."""
    p = plans.PLANS[code]
    return {"code": code, "name": p["name"], "usd": p["usd"], "days": days, "credits": int(p["credits"])}


def offer() -> dict:
    """The sign-up offer copy while PLANS_ONLY is on, in the shape of adoffer.offer(): what a new account starts
    with is the Starter trial, and the terms say when the card is charged."""
    import catalog
    r = catalog.rates()
    n = trial_credits(plans.PLANS["starter"])
    images = n // max(r["image"], 1)
    terms_line = (f"A card is needed to start, and the plan is charged when the {TRIAL_DAYS}-day trial ends unless "
                  "you cancel first.")
    return {"credits": n, "images": images, "video": n >= r["video5"], "ad": False, "card": True, "trial": True,
            "cta": f"Try it free for {TRIAL_DAYS} days",
            "body": f"Starter and Growth include a {TRIAL_DAYS}-day free trial with full access. {terms_line}",
            "terms": terms_line}


def checkout_note(code: str, days: int) -> str:
    """The line under the button on Stripe's page (custom_text.submit.message)."""
    p = plans.PLANS[code]
    if days:
        return (f"{days} days free with full access. Then ${p['usd']} a month with {int(p['credits']):,} credits a "
                f"month, until you cancel. Cancel any time in Billing; cancel before the trial ends and nothing "
                f"is charged.")
    return f"${p['usd']} a month with {int(p['credits']):,} credits a month, until you cancel. Cancel any time in Billing."

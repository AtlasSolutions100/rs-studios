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
  * About a day before a trial's first charge its owner gets one email saying what is charged, when, and how to
    cancel first (run_reminders, from the mail loop; not sent for a trial already set to cancel). The plan step
    and Billing show the trial as three dots: today $0, the reminder, the charge (timeline).
  * Accounts that were on Free pick a plan at their next visit to make anything
    new. Their library, downloads, sharing, Billing, the look switch, workspace
    deletion and logging out stay open (plan_gate_routes.ALLOWED).
  * Credit packs top up a plan; they are not sold without one.

Never asked for a plan: site admins; a workspace on a plan, an admin comp or a
free-month code; a checkout still confirming its payment; a subscription whose
payment failed (that is fixed on Billing, and the renewal hold already stops
spending); and a workspace that paid for a credit pack and still holds credits
(nothing that was paid for is taken away).

Welcome credits (Koby, Nov 13 2026). Not a free plan: a new sign-up can skip paying and the whole first run. Once
it confirms its email it gets WELCOME_CREDITS (500) once (grant_welcome, from verify.completed and a Google sign-up),
for its brand setup and first image ads. While it holds some it is not sent to pick a plan (exempt), but videos
wait for a plan (check_tool: they cost real vendor money). When they run out, the plan step comes back, with the
3-day trial. Once per workspace and per person (welcome_grants: the owner's email folded as for trials), never for
a team member joining a workspace, and never for a person who already had a trial or a paid plan.

Switching the flag off puts everything back: sign-ups get starter credits again
and nobody is asked for a plan. Trials already running carry on in Stripe.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from werkzeug.exceptions import HTTPException

import db
import plans
import preview

TRIAL_DAYS = 3
TRIAL_TEST_DAYS = 7         # the longer arm of the trial-length test (TRIAL_TEST; Koby, Nov 14 2026)
REFUND_DAYS = 7             # the first-charge refund window (REFUND_PROMISE; Koby, Nov 14 2026)
TRIAL_PLANS = ("starter", "growth")
TRIAL_PERCENT = 100         # of the plan's monthly credits, granted when the trial starts: full access
REMINDER_HOURS = 24         # the reminder email goes out about this long before a trial's first charge
MESSAGE = "Pick a plan to make something new."
VIDEO_MESSAGE = "Videos come with a plan. Your free credits cover your brand setup and image ads."
WELCOME_REASON = "welcome credits"


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


def trial_test_on() -> bool:
    """TRIAL_TEST (default off): the 7-day vs 3-day trial test. Half the new workspaces (even ids) get a
    TRIAL_TEST_DAYS trial, the rest keep TRIAL_DAYS. The length each trial started with goes on the Stripe
    subscription (metadata trial_days), so trial-to-paid can be compared by arm. Pages a logged-out visitor sees
    keep saying TRIAL_DAYS: the 7-day arm is told its own length on the plan step, at Stripe's checkout, in the
    reminder email and on Billing, so nobody is ever charged sooner than they were told."""
    return (os.environ.get("TRIAL_TEST") or "").strip().lower() in ("1", "on", "true", "yes")


def trial_len(ws=None) -> int:
    """The trial length for this workspace: TRIAL_TEST_DAYS for the test's 7-day arm, else TRIAL_DAYS. With no
    workspace (a logged-out page) it is TRIAL_DAYS."""
    wid = _key(ws, "id") if ws is not None and hasattr(ws, "keys") else None
    if trial_test_on() and wid is not None and int(wid) % 2 == 0:
        return TRIAL_TEST_DAYS
    return TRIAL_DAYS


def refund_on() -> bool:
    """REFUND_PROMISE (default off): the first-charge refund promise on pricing, the plan step, Stripe's checkout
    and the terms. Refunds are made by hand from Stripe while volume is small."""
    return (os.environ.get("REFUND_PROMISE") or "").strip().lower() in ("1", "on", "true", "yes")


def refund_line() -> str:
    """The refund promise in one sentence, or "" while REFUND_PROMISE is off."""
    if not refund_on():
        return ""
    return (f"Not happy after your first charge? Email us within {REFUND_DAYS} days of it for a full refund of "
            "that payment.")


def welcome_credits() -> int:
    """WELCOME_CREDITS: what a new account gets once it confirms its email (default 500; 0 turns it off)."""
    try:
        return max(0, int(os.environ.get("WELCOME_CREDITS", "500")))
    except ValueError:
        return 500


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
    return 0 if had_trial(conn, ws, user) else trial_len(ws)


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
    # On its welcome credits and still holds some: no plan yet (videos wait for one: check_tool).
    if int(_key(ws, "credits") or 0) > 0 and on_welcome(conn, ws):
        return True
    # Paid for credits before this was switched on and still holds some: they stay spendable.
    if int(_key(ws, "credits") or 0) > 0 and conn.execute(
            "SELECT 1 FROM purchases WHERE workspace_id=? AND status='paid' AND amount_cents>0 LIMIT 1",
            (ws["id"],)).fetchone() is not None:
        return True
    return False


def on_welcome(conn, ws) -> bool:
    """This workspace got the welcome credits and has no plan yet."""
    return ws is not None and plans.plan_code(ws) == "free" and conn.execute(
        "SELECT 1 FROM welcome_grants WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone() is not None


def welcome_left(conn, ws) -> int | None:
    """The credits a workspace on its welcome credits still has; None when it isn't on them."""
    if not on() or not on_welcome(conn, ws):
        return None
    row = conn.execute("SELECT credits FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
    return int(row["credits"] or 0) if row is not None else 0


def grant_welcome(conn, ws, user) -> int:
    """The member confirmed their email: give their workspace the welcome credits, once. Only the owner of a
    workspace with no plan, never paid, while PLANS_ONLY is on; not again for the same workspace or the same person
    (email folded), and not for a person who already had a trial. No commit. Returns the credits granted."""
    n = welcome_credits()
    if not on() or not n or ws is None or user is None or _key(user, "role") != "owner":
        return 0
    if plans.plan_code(ws) != "free" or _key(ws, "stripe_subscription_id"):
        return 0
    email = _email_norm(user)
    if had_trial(conn, ws, user) or conn.execute(
            "SELECT 1 FROM purchases WHERE workspace_id=? AND status='paid' LIMIT 1", (ws["id"],)).fetchone():
        return 0
    if email and conn.execute("SELECT 1 FROM welcome_grants WHERE email_norm=? LIMIT 1", (email,)).fetchone():
        return 0
    cur = conn.execute("INSERT OR IGNORE INTO welcome_grants (workspace_id, user_id, email_norm, credits, granted_at) "
                       "VALUES (?,?,?,?,?)", (ws["id"], user["id"], email, n, db.now()))
    if cur.rowcount != 1:
        return 0
    db.adjust_credits(conn, ws["id"], n, WELCOME_REASON)
    return n


def check_tool(conn, ws, tool, user=None):
    """Raise PlanRequired (VIDEO_MESSAGE) for a video on a workspace that is on its welcome credits."""
    if not on() or ws is None or (user is not None and _key(user, "is_admin")):
        return
    import verify
    if verify.is_video(tool) and on_welcome(conn, ws):
        raise PlanRequired(VIDEO_MESSAGE)


SPENT_MESSAGE = (f"You've used your free credits. Pick a plan to keep going: Starter and Growth start with a "
                 f"{TRIAL_DAYS}-day free trial.")


def spent_message(ws=None) -> str:
    """SPENT_MESSAGE with this workspace's trial length (trial_len)."""
    return SPENT_MESSAGE.replace(f"{TRIAL_DAYS}-day", f"{trial_len(ws)}-day")


def welcome_spent(ws):
    """Raise PlanRequired (SPENT_MESSAGE) where a spend was refused for want of credits on a workspace that is on
    its welcome credits: the next step is a plan, not a credit pack (packs need a plan)."""
    if not on() or ws is None:
        return
    with db.connect() as conn:
        if on_welcome(conn, ws):
            raise PlanRequired(spent_message(ws))


def upgrade_instead(ws) -> bool:
    """Short of credits with no plan (on the welcome credits): the way on is a plan, not a pack (packs need one)."""
    return on() and ws is not None and plans.plan_code(ws) == "free"


def short_message(total: int, balance: int, ws=None) -> str:
    return (f"That needs {total:,} credits and you have {balance:,}. Pick a plan to keep going: Starter and Growth "
            f"start with a {trial_len(ws)}-day free trial.")


def needs_plan(conn, ws, user=None) -> bool:
    return on() and not exempt(conn, ws, user)


FREE_PASS = "first_free_pass"     # flask.g: the workspace whose free first result this request is making


@contextmanager
def free_pass(ws_id: int):
    """While the free first result is made (first_ads.make), its ads get past the plan check, in this request and for
    this workspace only. Everything else the workspace tries still waits for a plan."""
    from flask import g
    before = g.get(FREE_PASS)
    setattr(g, FREE_PASS, ws_id)
    try:
        yield
    finally:
        setattr(g, FREE_PASS, before)


def _passing(ws) -> bool:
    from flask import g, has_app_context
    return has_app_context() and g.get(FREE_PASS) is not None and g.get(FREE_PASS) == _key(ws, "id")


def check(ws, user=None):
    """Raise PlanRequired where a spend would start on a workspace that has to pick a plan first (not for the free
    first result's own ads: free_pass)."""
    if not on() or ws is None or _passing(ws):
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


def timeline(start_iso: str | None, end_iso: str | None, usd=None, *, canceling: bool = False,
             days: int | None = None) -> list[dict]:
    """The trial as three dots (trial_line in web/_price.html): the start ($0, full access), the reminder email
    about a day before it ends (run_reminders), and the charge. With no dates it is the picture before checkout:
    today, then TRIAL_DAYS from now. Each step: {"label" or "iso", "amount", "what", "now"}. The reminder is only
    promised while email is connected; a trial set to cancel says so instead."""
    import mailer
    now = datetime.now(timezone.utc).replace(microsecond=0)
    before = not start_iso
    try:
        start = now if before else datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso) if end_iso else start + timedelta(days=days or TRIAL_DAYS)
    except ValueError:
        start, end = now, now + timedelta(days=days or TRIAL_DAYS)
    day_before = (end - timedelta(hours=REMINDER_HOURS)).isoformat()
    first = {"label": "Today" if before else None, "iso": start.isoformat(), "amount": "$0", "what": "Full access",
             "now": True}
    if canceling:
        return [first, {"iso": day_before, "what": "Set to cancel"},
                {"iso": end.isoformat(), "what": "Trial ends, $0 charged"}]
    middle = "Reminder email" if mailer.configured() else "Cancel any time before"
    charge = f"${usd} charged" if usd else "Your plan's price charged"
    return [first, {"iso": day_before, "what": middle}, {"iso": end.isoformat(), "what": charge}]


def timeline_for(conn, ws) -> list[dict] | None:
    """The timeline of the trial this workspace is in (Billing), or None."""
    t = trial_view(ws)
    if t is None:
        return None
    row = conn.execute("SELECT started_at, ends_at FROM plan_trials WHERE workspace_id=? AND subscription_id=?",
                       (ws["id"], _key(ws, "stripe_subscription_id"))).fetchone()
    start = row["started_at"] if row is not None else None
    return timeline(start or db.now(), (row["ends_at"] if row is not None else None) or t["ends_at"], t["usd"],
                    canceling=t["canceling"])


# ---------------------------------------------------------------- the reminder before the first charge
def reminders_due(conn, now=None) -> list:
    """Trials whose first charge is within REMINDER_HOURS: still trialing on that subscription, not set to cancel,
    not reminded yet."""
    now = now or datetime.now(timezone.utc)
    lo, hi = now.replace(microsecond=0).isoformat(), (now + timedelta(hours=REMINDER_HOURS)).replace(microsecond=0).isoformat()
    return conn.execute(
        "SELECT t.* FROM plan_trials t JOIN workspaces w ON w.id=t.workspace_id "
        "WHERE t.reminded_at IS NULL AND t.ends_at IS NOT NULL AND t.ends_at>? AND t.ends_at<=? "
        "AND w.plan_status='trialing' AND w.stripe_subscription_id=t.subscription_id "
        "AND COALESCE(w.plan_cancel_at_period_end, 0)=0 ORDER BY t.ends_at", (lo, hi)).fetchall()


def reminder_copy(plan: dict, hours: int, billing_url: str, days: int = TRIAL_DAYS) -> tuple[str, str, str]:
    """(subject, text, html) of the reminder: what is charged, when, and how to cancel first."""
    usd, name = plan["usd"], plan["name"]
    when = f"about {hours} hour{'' if hours == 1 else 's'}"
    subject = f"Your Campaignify trial ends in {when}"
    lines = [f"Your {days}-day free trial of Campaignify {name} ends in {when}.",
             f"When it does, your card is charged ${usd} for the first month, and ${usd} each month after that until "
             f"you cancel. You carry on with the credits you have now.",
             "To keep your plan, there is nothing to do.",
             f"To cancel before you are charged, open Billing and choose Manage or cancel plan: {billing_url}",
             "Questions? Reply to this email."]
    text = "\n\n".join(lines)
    html = ('<div style="font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#141414;max-width:520px">'
            + "".join(f"<p>{line}</p>" for line in lines[:3])
            + f'<p>To cancel before you are charged, open <a href="{billing_url}">Billing</a> and choose Manage or '
              f"cancel plan.</p><p>{lines[4]}</p></div>")
    return subject, text, html


def remind(conn, trial, now=None) -> bool:
    """Send one trial's reminder to its owner. The trial is marked first, so nobody is mailed twice; an email the
    provider defers is kept and sent again by the mailer."""
    import logging

    import mailer
    import verify
    now = now or datetime.now(timezone.utc)
    p = plans.PLANS.get(trial["plan"])
    user = conn.execute("SELECT * FROM users WHERE id=?", (trial["user_id"],)).fetchone() if trial["user_id"] else None
    if user is None:
        user = conn.execute("SELECT * FROM users WHERE workspace_id=? ORDER BY (role='owner') DESC, id LIMIT 1",
                            (trial["workspace_id"],)).fetchone()
    if conn.execute("UPDATE plan_trials SET reminded_at=? WHERE id=? AND reminded_at IS NULL",
                    (db.now(), trial["id"])).rowcount != 1:
        conn.commit()
        return False
    conn.commit()
    if p is None or user is None or "@" not in (user["email"] or ""):
        return False
    hours = max(1, round((datetime.fromisoformat(trial["ends_at"]) - now).total_seconds() / 3600))
    days = TRIAL_DAYS
    try:        # the length this trial really ran (the 7-day arm of TRIAL_TEST says 7)
        span = datetime.fromisoformat(trial["ends_at"]) - datetime.fromisoformat(trial["started_at"])
        days = max(1, round(span.total_seconds() / 86400))
    except (TypeError, ValueError, KeyError, IndexError):
        pass
    subject, text, html = reminder_copy(p, hours, verify.public_url() + "/app/billing", days)
    try:
        mailer.send(frm=verify.from_address(), to=user["email"], subject=subject, html=html, text=text,
                    reply_to=verify.reply_to(), tags={"kind": "trial_reminder"},
                    idempotency_key=f"trial-reminder-{trial['id']}")
        return True
    except mailer.MailError as e:
        logging.getLogger("campaignify").warning("trial reminder for workspace %s not sent: %s", trial["workspace_id"], e)
        return False


def run_reminders(now=None) -> int:
    """Send every trial reminder that is due (nudges.mail_tick, every few minutes). Returns how many went out."""
    import mailer
    if not mailer.configured():
        return 0
    sent = 0
    with db.connect() as conn:
        for t in reminders_due(conn, now):
            sent += 1 if remind(conn, t, now) else 0
    return sent


def terms(code: str, days: int) -> dict:
    """What picking plan `code` means in money, for the plan page."""
    p = plans.PLANS[code]
    return {"code": code, "name": p["name"], "usd": p["usd"], "days": days, "credits": int(p["credits"])}


def offer() -> dict:
    """The sign-up offer copy while PLANS_ONLY is on, in the shape of adoffer.offer(): what a new account starts
    with is the Starter trial, and the terms say when the card is charged."""
    import catalog
    r = catalog.rates()
    w = welcome_credits()
    if w:
        # Not a free plan (Koby, Nov 13 2026): free credits once the email is confirmed, no card; then a plan.
        terms_line = (f"No card to start. Starter and Growth begin with a {TRIAL_DAYS}-day free trial and are charged "
                      "when it ends, unless you cancel first.")
        return {"credits": w, "images": w // max(r["image"], 1), "video": False, "ad": False, "card": False,
                "trial": True, "cta": "Start free",
                # the home page's line under the button (Koby, Nov 13 2026: one short line); the plan step and
                # Stripe's checkout still show `terms` (when the card is charged) before anything is charged
                "body": f"No card to start. {TRIAL_DAYS}-day free trial when you're ready.",
                "terms": terms_line}
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
    refund = (" " + refund_line()) if refund_on() else ""
    if days:
        return (f"{days} days free with full access. Then ${p['usd']} a month with {int(p['credits']):,} credits a "
                f"month, until you cancel. Cancel any time in Billing; cancel before the trial ends and nothing "
                f"is charged.{refund}")
    return (f"${p['usd']} a month with {int(p['credits']):,} credits a month, until you cancel. Cancel any time in "
            f"Billing.{refund}")

"""The "Your ads are ready" email: one per member, when their first batch finishes.

A first batch takes a minute (image ads) to a few minutes (video). People leave
the tab; without a note they don't come back to see what they made. So when the
last job of a member's FIRST batch settles and at least one ad is done, the
member who made it gets one short email with a link to the batch.

Rules: once per member (a row in first_run_nudges with step READY_STEP, written
before the send, so a failing mailer never mails anyone twice); only for the
member's first batch; never while a job in it is still pending or running; never
when every job failed (the page already says so and the credits went back); only
when email is connected. It is an account email (mailer kind "ads_ready",
transactional: it answers something the member just asked for). ADS_READY_EMAIL=0
switches it off.

worker.py calls on_job_settled() after a job is marked done or failed. Nothing
here ever raises into the worker.
"""
import html as _html
import json
import logging
import os

import db
import mailer
import verify

log = logging.getLogger("campaignify")

READY_STEP = 10     # first_run_nudges.step for this email (1 and 2 are the "your free ads are waiting" nudges)
KIND = "ads_ready"


def enabled() -> bool:
    return os.environ.get("ADS_READY_EMAIL", "1") != "0"


def _batch_ready(conn, batch_id):
    """(batch row, done jobs) when this batch has just settled with something to show, else None."""
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if b is None or not b["user_id"]:
        return None
    if conn.execute("SELECT 1 FROM batches WHERE workspace_id=? AND user_id=? AND id<? LIMIT 1",
                    (b["workspace_id"], b["user_id"], b["id"])).fetchone():
        return None         # not this member's first batch
    jobs = conn.execute("SELECT id, status, tool, params_json FROM jobs WHERE batch_id=? AND deleted_at IS NULL",
                        (b["id"],)).fetchall()
    if not jobs or any(j["status"] in ("pending", "running") for j in jobs):
        return None
    done = [j for j in jobs if j["status"] == "done"]
    return (b, done) if done else None


def copy(batch, done) -> tuple[str, str, str]:
    """(subject, text, html). Says how many ads and of what kind, and links to the batch."""
    n = 0
    video = False
    for j in done:
        try:
            p = json.loads(j["params_json"] or "{}")
        except (ValueError, TypeError):
            p = {}
        video = video or j["tool"] != "ad_image"
        n += max(1, int(p.get("batch") or 1)) if j["tool"] == "ad_image" else 1
    what = ("video" if video else "ad") if n == 1 else ("videos" if video else "ads")
    subject = f"Your {what} {'is' if n == 1 else 'are'} ready"
    link = f"{verify.public_url()}/app/create/batch/{batch['id']}"
    lead = (f"Your {what} {'is' if n == 1 else 'are'} ready in Campaignify"
            + (f": {n} of them." if n > 1 else "."))
    do = ("Open the batch to look, download the ones you like, and make more from the same product. "
          "If one isn't right, say so on the ad: it helps the next one.")
    text = f"Hi,\n\n{lead}\n\n{do}\n\n{link}\n\nYou'll need to be logged in to open it.\n\nCampaignify"
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            f"<p>Hi,</p><p>{_html.escape(lead)}</p><p>{_html.escape(do)}</p>"
            f'<p><a href="{_html.escape(link)}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            f'border-radius:8px;text-decoration:none;font-weight:600">See your {what}</a></p>'
            f'<p style="font-size:13px;color:#555">Or paste this into your browser (you\'ll need to be logged in):<br>'
            f"{_html.escape(link)}</p></div>")
    return subject, text, html


def on_job_settled(conn, job) -> bool:
    """Send the email if this job was the last of its member's first batch. True when one was sent. Never raises."""
    try:
        if not enabled() or not mailer.configured():
            return False
        bid = job["batch_id"] if "batch_id" in job.keys() else None
        if not bid:
            return False
        ready = _batch_ready(conn, bid)
        if ready is None:
            return False
        batch, done = ready
        user = conn.execute("SELECT * FROM users WHERE id=? AND workspace_id=?",
                            (batch["user_id"], batch["workspace_id"])).fetchone()
        if user is None or not user["email"]:
            return False
        # Claim it first: only the caller whose INSERT lands sends, and a failed send is not retried from here.
        cur = conn.execute("INSERT OR IGNORE INTO first_run_nudges (user_id, step, sent_at) VALUES (?,?,?)",
                           (user["id"], READY_STEP, db.now()))
        conn.commit()
        if cur.rowcount != 1:
            return False
        subject, text, html = copy(batch, done)
        mailer.send(frm=verify.from_address(), to=user["email"], subject=subject, html=html, text=text,
                    reply_to=verify.reply_to(), tags={"kind": KIND})
        return True
    except mailer.MailError as e:
        log.warning("ads-ready email for job %s not sent: %s", job["id"], e)
        return False
    except Exception as e:  # noqa: BLE001 — a notification never fails a job
        log.warning("ads-ready email for job %s skipped: %s", job["id"], str(e)[:160])
        return False

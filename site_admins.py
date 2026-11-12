"""Site admins: how one admin makes another person's account a site admin, and takes it back (Koby, Nov 11 2026).

A site admin (users.is_admin) opens /admin: every workspace, credits, plans and prices, and may preview the new look
(ui_v2.can_preview). Until now an account could only become one through an owner setup link (owners.py, for an
address with no account yet) or the one-time first-owner setting (db.py). This is the plain case: an admin names an
account that already exists.

    POST /admin/admins     action=add      email, sure=1     that account becomes a site admin
                           action=remove   user=<id>         it stops being one (never your own)

Rules:
  * only a site admin, with the session's CSRF token like every form (app.py checks it for every POST);
  * the email is matched whole, without regard to capitals, to an account whose email is confirmed. A sign-up nobody
    confirmed is refused: until the address is confirmed, anyone could have typed it;
  * `sure` must be ticked; the card says what a site admin can do;
  * nobody removes their own access, so there is always a site admin left;
  * a few changes an hour for each admin (PER_HOUR);
  * a change is never silent: it is logged (user ids only, no address) and, when email is connected, every site
    admin and the person concerned get one plain email saying who changed what.

No address is written in the source (SEC-9): the admin types it.
"""
import html as htmllib
import logging

from flask import Blueprint, flash, g, redirect, request, url_for

import db
from core import admin_required

bp = Blueprint("site_admins", __name__)
log = logging.getLogger("campaignify.site_admins")

PER_HOUR = 10
CAN = "A site admin can open this page: every workspace, its credits and plan, and the prices. They can also preview the new look."


def listed(conn) -> list:
    """The site admins, the oldest account first, with their workspace's name."""
    return conn.execute("SELECT u.id, u.email, u.name, w.name ws_name FROM users u LEFT JOIN workspaces w ON "
                        "w.id=u.workspace_id WHERE u.is_admin=1 ORDER BY u.id").fetchall()


def _notify(conn, actor, target, added: bool) -> int:
    """One plain email to every site admin and to the person concerned. Returns how many went out; a mail problem
    never undoes the change."""
    import mailer
    import verify
    if not mailer.configured():
        return 0
    what = "is now a site admin on Campaignify" if added else "is no longer a site admin on Campaignify"
    lines = [f"{target['email']} {what}.", f"Changed by {actor['email']}."]
    if added:
        lines.append(CAN)
        lines.append("To see the new look: log in, then open any page with ?ui=v2 at the end of its address.")
    lines.append("If this was not meant to happen, a site admin can change it on the admin page, under Site admins.")
    text = "\n\n".join(lines)
    body = ('<div style="font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#141414;max-width:560px">'
            + "".join(f"<p>{htmllib.escape(x)}</p>" for x in lines) + "</div>")
    to = {r["email"] for r in listed(conn)} | {target["email"], actor["email"]}
    sent = 0
    for addr in sorted(to):
        try:
            mailer.send(frm=verify.from_address(), to=addr, subject=f"Site admin {'added' if added else 'removed'} on Campaignify",
                        html=body, text=text, tags={"kind": "site_admin_change"})
            sent += 1
        except mailer.MailError as e:
            log.warning("site admin notice not sent: %s", str(e)[:120])
    return sent


@bp.route("/admin/admins", methods=["POST"])
@admin_required
def change():
    back = redirect(url_for("admin") + "#admins")
    action = request.form.get("action") or ""
    key = f"site-admin:{g.user['id']}"
    if action not in ("add", "remove"):
        return back
    if db.throttled(key, PER_HOUR, 3600):
        flash("That is a lot of admin changes in an hour. Try again later.")
        return back
    with db.connect() as conn:
        if action == "add":
            email = (request.form.get("email") or "").strip().lower()
            if not email or "@" not in email:
                flash("Type the email of the account to make a site admin.")
                return back
            if request.form.get("sure") != "1":
                flash("Tick the box to confirm. " + CAN)
                return back
            u = conn.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
            if u is None:
                flash("No account has that email. They sign up first, then you add them here.")
                return back
            if u["is_admin"]:
                flash(f"{u['email']} is already a site admin.")
                return back
            if not u["email_verified_at"]:
                flash(f"{u['email']} has not confirmed that email yet. They open the link in their confirmation email "
                      "first.")
                return back
            conn.execute("UPDATE users SET is_admin=1 WHERE id=? AND is_admin=0", (u["id"],))
            conn.commit()
            db.hit(key)
            log.warning("site admin added: user %s by user %s", u["id"], g.user["id"])
            _notify(conn, g.user, u, True)
            flash(f"{u['email']} is now a site admin. To see the new look they log in and open any page with ?ui=v2 at "
                  "the end of its address.")
            return back
        uid = request.form.get("user", type=int)
        u = conn.execute("SELECT * FROM users WHERE id=? AND is_admin=1", (uid or 0,)).fetchone()
        if u is None:
            return back
        if u["id"] == g.user["id"]:
            flash("You can't remove your own admin access. Another site admin can.")
            return back
        conn.execute("UPDATE users SET is_admin=0 WHERE id=?", (u["id"],))
        conn.commit()
        db.hit(key)
        log.warning("site admin removed: user %s by user %s", u["id"], g.user["id"])
        _notify(conn, g.user, u, False)
        flash(f"{u['email']} is no longer a site admin.")
    return back

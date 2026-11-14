"""Done-for-you follow-up from /admin/signups (Koby approved the wording on Nov 14 2026): one admin button per sign-up
that never got a finished ad. It sends one personal email through outreach.send, so the daily cap, the do-not-contact
list, the 14-day rule and the CAN-SPAM footer all apply as on /admin/outreach.

    Email A  the workspace has a product page we read (first_ads.link_of): its free first ads (first_ads.make, the
             same 3 image ads a new sign-up gets before the plan step, once per workspace and per person) are made in
             THEIR workspace first, then the email links to them in their account (onboarding.first_result).
    Email B  no product page: an offer to make 3 ads from one, if they reply with a link.

Who gets a button: the first member of a workspace (not someone who joined a team), not a site admin, not paying
(verify.paid), and no finished ad in the workspace. One follow-up per sign-up: a row in signup_followups, which also
lets the page say "emailed <date>".

The page shows masked emails (admin_funnel.mask_email); the address is looked up here by user id, server-side, and
never put on the page, in a flash or in a URL.

The ads are made with the admin as the acting member (g.user) inside the member's workspace (g.ws): the plan and
email checks are the admin's, so a sign-up who never confirmed their email can still be sent ads made for them, and
the jobs stay under the admin's id, so no automated "Your ads are ready" email goes out on top of this one. The free
first result's own rules (once per workspace and per person, exactly what the ads cost) are first_ads'.
"""
import contextlib
import logging

from flask import Blueprint, flash, g, redirect, url_for

import db
import first_ads
import onboarding
import outreach
import products
import verify
from core import admin_required

log = logging.getLogger("campaignify")
bp = Blueprint("followup", __name__)

KIND_ADS, KIND_ASK = "A", "B"

SUBJECT_A = "I made [n] ads from your [product] page"
BODY_A = """Hi [first name],

You signed up for Campaignify but didn't get as far as making an ad, so I made three from your product page ([link]). Here they are: [ads].

They're yours to use. If one looks right, reply and I'll turn it into a short video with an AI presenter.

Koby"""

SUBJECT_B = "Want [n] free ads for [brand]?"
BODY_B = """Hi [first name],

You signed up for Campaignify but didn't get to make an ad. Reply with a link to one product page and I'll make you [n] image ads from it, free, usually within a day. No card and nothing to set up.

Koby"""


class Refused(ValueError):
    """Not sent (the message says why, with no address in it)."""


def _fill(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace(f"[{k}]", v)
    return text


def first_name(user) -> str:
    """The member's first name, or "" (then the email says "Hi there,")."""
    parts = " ".join(str(user["name"] or "").split()).split(" ") if user is not None else []
    return parts[0] if parts and parts[0] and "@" not in parts[0] else ""


def _greeting(text: str, user) -> str:
    name = first_name(user)
    return text.replace("Hi [first name],", f"Hi {name}," if name else "Hi there,")


def _ws(conn, ws_id):
    return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def _product(conn, ws):
    return products.get_product(conn, ws["id"], ws["product_id"]) if ws is not None and ws["product_id"] else None


def _page_link(conn, ws, user) -> str:
    """The product page we read for this workspace (its imported product), or ""."""
    if _product(conn, ws) is None:
        return ""
    return first_ads.link_of(conn, ws, user)


def sent_row(conn, user_id):
    return conn.execute("SELECT * FROM signup_followups WHERE user_id=?", (user_id,)).fetchone()


def eligible(conn, user) -> tuple[bool, str]:
    """(may get a follow-up, why not). The first member of a workspace, not a site admin, not paying, no finished ad,
    not followed up before."""
    if user is None:
        return False, "No such sign-up."
    if user["is_admin"]:
        return False, "An owner's account."
    first = conn.execute("SELECT MIN(id) FROM users WHERE workspace_id=?", (user["workspace_id"],)).fetchone()[0]
    if first != user["id"]:
        return False, "Joined a team: the workspace's owner is the one to follow up."
    ws = _ws(conn, user["workspace_id"])
    if ws is None or verify.paid(conn, ws):
        return False, "Paying already."
    if conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? AND status='done' LIMIT 1", (ws["id"],)).fetchone():
        return False, "Already has a finished ad."
    row = sent_row(conn, user["id"])
    if row is not None and row["status"] == "sent":
        return False, f"Emailed {row['sent_at'][:10]}."
    return True, ""


def kind_for(conn, user) -> str:
    """A when we read a product page for this workspace and its free first ads are still open, or were made by an
    earlier follow-up whose email did not go out; else B."""
    ws = _ws(conn, user["workspace_id"])
    if not _page_link(conn, ws, user):
        return KIND_ASK
    row = sent_row(conn, user["id"])
    if row is not None and row["made_ads"]:
        return KIND_ADS
    return KIND_ADS if first_ads.free_open(conn, ws, user) else KIND_ASK


def states(conn, user_ids) -> dict:
    """For the page: {user id: {"can", "kind", "why", "sent_at", "status"}}."""
    out = {}
    for uid in user_ids:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        ok, why = eligible(conn, u)
        row = sent_row(conn, uid)
        out[uid] = {"can": ok, "why": why, "kind": kind_for(conn, u) if ok else "", "n": first_ads.FREE_IMAGES,
                    "sent_at": row["sent_at"] if row is not None and row["status"] == "sent" else "",
                    "status": row["status"] if row is not None else ""}
    return out


def compose(conn, user, kind) -> tuple[str, str, str]:
    """(subject, body, brand) for this sign-up, every bracket filled."""
    ws = _ws(conn, user["workspace_id"])
    p = _product(conn, ws)
    b = products.get_brand(conn, ws["id"], p["brand_id"] if p is not None else ws["brand_id"]) \
        if (p is not None or ws["brand_id"]) else None
    brand = (b["brand_name"] if b is not None else "") or onboarding.answers(user).get("company") or ""
    n = str(first_ads.FREE_IMAGES)
    if kind == KIND_ADS:
        values = {"n": n, "product": p["name"], "link": _page_link(conn, ws, user),
                  "ads": verify.public_url() + url_for("onboarding.first_result")}
        return _fill(SUBJECT_A, values), _fill(_greeting(BODY_A, user), values), brand or p["name"]
    values = {"n": n, "brand": brand or "your product"}
    return _fill(SUBJECT_B, values), _fill(_greeting(BODY_B, user), values), brand


@contextlib.contextmanager
def _in_workspace(ws):
    """Act in the member's workspace for this request, the admin still the acting member."""
    before = g.get("ws")
    g.ws = ws
    try:
        yield
    finally:
        g.ws = before


def _make_ads(conn, user) -> None:
    """The free first ads in the member's workspace (first_ads.make). Raises Refused when they could not start."""
    ws = _ws(conn, user["workspace_id"])
    p = _product(conn, ws)
    with _in_workspace(ws):
        out = first_ads.make(conn, ws, g.user, p, {})
    if not out.get("ok") or not out.get("free"):
        raise Refused("The ads could not be made: " + (out.get("error") or "try again in a minute") +
                      " Nothing was sent.")


def _masked(text: str, addr: str) -> str:
    import admin_funnel
    return (text or "").replace(addr, admin_funnel.mask_email(addr)) if addr else (text or "")


def follow_up(conn, user_id: int, admin_id: int | None = None) -> dict:
    """Make the ads when there is a page to make them from, then send the email. {"kind", "status", "made"}.
    Raises Refused (with no address in the message): not eligible, a guard rail, or ads that could not start."""
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    ok, why = eligible(conn, user)
    if not ok:
        raise Refused(why)
    addr = outreach.norm_email(user["email"])
    kind = kind_for(conn, user)
    subject, body, brand = compose(conn, user, kind)
    try:        # the guard rails first, so no ads are made for an email that can't go out
        outreach.check(conn, to=addr, subject=subject, body=body, follow_up=False, test=False)
    except outreach.Refused as e:
        raise Refused(_masked(str(e), addr)) from None
    made = False
    row = sent_row(conn, user["id"])
    if kind == KIND_ADS and not (row is not None and row["made_ads"]):
        _make_ads(conn, user)
        made = True
        conn.execute("INSERT INTO signup_followups (user_id, workspace_id, kind, made_ads, status, admin_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET kind=excluded.kind, made_ads=1",
                     (user["id"], user["workspace_id"], kind, 1, "made", admin_id, db.now()))
        conn.commit()
    try:
        sent = outreach.send(conn, to=addr, brand=brand, subject=subject, body=body, user_id=admin_id)
    except outreach.Refused as e:
        raise Refused(_masked(str(e), addr)) from None
    status = sent["status"]
    conn.execute("INSERT INTO signup_followups (user_id, workspace_id, kind, made_ads, outreach_id, status, admin_id, "
                 "created_at, sent_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET kind=excluded.kind, "
                 "outreach_id=excluded.outreach_id, status=excluded.status, sent_at=excluded.sent_at",
                 (user["id"], user["workspace_id"], kind, int(made), sent["id"], status, admin_id, db.now(),
                  db.now() if status == "sent" else None))
    conn.commit()
    return {"kind": kind, "status": status, "made": made}


@bp.route("/admin/signups/<int:uid>/follow-up", methods=["POST"])
@admin_required
def send(uid):
    with db.connect() as conn:
        try:
            out = follow_up(conn, uid, g.user["id"])
        except Refused as e:
            flash(f"Not sent: {e}")
            return redirect(url_for("admin_signups") + "#signups")
    if out["status"] != "sent":
        flash("The email didn't go out (the mail service refused it). Press the button again to retry; "
              + ("the ads are already made, so they won't be made twice." if out["kind"] == KIND_ADS else ""))
    elif out["kind"] == KIND_ADS:
        flash(f"Made {first_ads.FREE_IMAGES} free image ads in their workspace and sent email A.", "ok")
    else:
        flash("Sent email B (no product page yet).", "ok")
    return redirect(url_for("admin_signups") + "#signups")


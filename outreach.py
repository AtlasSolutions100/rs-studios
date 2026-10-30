"""Owner outreach: a few personal, one-to-one emails a day to small brands,
each with a free sample ad made for them (admin-only, /admin/outreach).

Every email goes out from "Koby Stull" <hello@RS_MAIL_DOMAIN> (RS_OUTREACH_FROM
replaces the whole From) with replies to hello@ the From's domain, and carries
the CAN-SPAM footer: Campaignify's postal address and a "reply 'no thanks'"
line. Guard rails, all checked before anything is sent:

- an address or domain on the do-not-contact list is refused;
- the same address twice within REPEAT_DAYS is refused unless it's a follow-up;
- at most DAILY_CAP sends in any 24 hours (test sends to yourself don't count);
- a subject or body with an unfilled {placeholder} is refused.

Every attempt is logged in outreach_log (sent, or the error).
"""
import hashlib
import html
import os
import re
from datetime import datetime, timedelta, timezone

import db
import mailer
import messaging

DAILY_CAP = 20
REPEAT_DAYS = 14
OPT_OUT_LINE = "Not interested? Reply 'no thanks' and I won't email again."
PLACEHOLDER = re.compile(r"\{[^{}\n]*\}")
EMAIL = re.compile(r"^[^@\s,;<>\"']+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


class Refused(ValueError):
    """A send the guard rails stop (the message says why)."""


# ---------------------------------------------------------------- addresses

def from_header() -> str:
    custom = (os.environ.get("RS_OUTREACH_FROM") or "").strip()
    return custom or f'"Koby Stull" <hello@{mailer.domain()}>'


def reply_to() -> str:
    """hello@ the same domain the email comes from."""
    m = re.search(r"@([^>\s]+)>?\s*$", from_header())
    return f"hello@{(m.group(1) if m else mailer.domain()).lower()}"


def postal_address() -> str:
    return (messaging.CAMPAIGNIFY_ADDRESS or "").strip()


def norm_email(s: str) -> str:
    return (s or "").strip().lower()


def norm_entry(s: str) -> str:
    """A suppression entry: a whole address, or a domain ("acme.com", "@acme.com"
    or a pasted URL all mean the domain)."""
    s = (s or "").strip().lower()
    s = re.sub(r"^[a-z]+://", "", s).split("/", 1)[0]
    s = s.removeprefix("@")
    return s if "@" in s else s.removeprefix("www.")


def valid_entry(s: str) -> bool:
    return bool(EMAIL.match(s)) if "@" in s else bool(DOMAIN.match(s))


# ---------------------------------------------------------------- the email

def _squash(s: str) -> str:
    return " ".join((s or "").split()).lower()


def footer_text(body: str) -> str:
    """What still has to be added to the body for the footer (each part once)."""
    have = _squash(body)
    parts = []
    addr = postal_address()
    if addr and _squash(addr) not in have:
        parts.append(f"Campaignify · {addr}")
    if _squash(OPT_OUT_LINE) not in have:
        parts.append(OPT_OUT_LINE)
    return "\n".join(parts)


def compose(body: str) -> tuple[str, str]:
    """(text, html): the body as typed plus the footer; HTML is escaped paragraphs
    (blank lines split paragraphs, single newlines become <br>)."""
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    foot = footer_text(body)
    text = body + ("\n\n--\n" + foot if foot else "") + "\n"

    def paras(s):
        return [p.strip() for p in re.split(r"\n\s*\n", s) if p.strip()]

    out = [f'<p style="margin:0 0 14px">{html.escape(p).replace(chr(10), "<br>")}</p>' for p in paras(body)]
    if foot:
        out.append('<p style="margin:24px 0 0;color:#777;font-size:12px;line-height:1.5">'
                   + html.escape(foot).replace("\n", "<br>") + "</p>")
    page = ('<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;'
            'line-height:1.55;color:#222;max-width:600px">' + "".join(out) + "</div>")
    return text, page


def placeholders(*parts: str) -> list[str]:
    seen = []
    for p in parts:
        for m in PLACEHOLDER.findall(p or ""):
            if m not in seen:
                seen.append(m)
    return seen


# ---------------------------------------------------------------- guard rails

def suppressed(conn, addr: str) -> str | None:
    """The do-not-contact entry that covers this address (the address itself, its
    domain, or a parent domain), else None."""
    addr = norm_email(addr)
    dom = addr.rsplit("@", 1)[-1]
    keys = [addr] + [".".join(dom.split(".")[i:]) for i in range(len(dom.split(".")) - 1)]
    row = conn.execute(f"SELECT entry FROM outreach_suppressions WHERE entry IN ({','.join('?' * len(keys))}) "
                       "ORDER BY length(entry) DESC LIMIT 1", keys).fetchone()
    return row["entry"] if row else None


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).replace(microsecond=0).isoformat()


def sent_last_day(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM outreach_log WHERE is_test=0 AND status IN ('sending','sent') "
                        "AND created_at > ?", (_ago(hours=24),)).fetchone()[0]


def last_sent_to(conn, addr: str):
    return conn.execute("SELECT * FROM outreach_log WHERE to_addr=? AND is_test=0 AND status IN ('sending','sent') "
                        "AND created_at > ? ORDER BY id DESC LIMIT 1", (norm_email(addr), _ago(days=REPEAT_DAYS))).fetchone()


def check(conn, *, to: str, subject: str, body: str, follow_up: bool, test: bool) -> None:
    """Raise Refused if this send isn't allowed."""
    if not mailer.configured():
        raise Refused("Email sending isn't connected (RESEND_API_KEY and RS_MAIL_DOMAIN).")
    if not EMAIL.match(to or ""):
        raise Refused("Put one email address in To.")
    if not (subject or "").strip() or not (body or "").strip():
        raise Refused("Add a subject and a body.")
    left = placeholders(subject, body)
    if left:
        raise Refused("Fill in " + ", ".join(left[:5]) + " first (nothing was sent).")
    if test:
        return
    hit = suppressed(conn, to)
    if hit:
        raise Refused(f"{to} is on the do-not-contact list ({hit}). Nothing was sent.")
    if sent_last_day(conn) >= DAILY_CAP:
        raise Refused(f"That's {DAILY_CAP} outreach emails in the last 24 hours, the daily cap. Send the rest tomorrow.")
    prev = last_sent_to(conn, to)
    if prev and not follow_up:
        raise Refused(f"You emailed {to} on {prev['created_at'][:10]}. Wait {REPEAT_DAYS} days, "
                      "or tick Follow-up if this is one.")


# ---------------------------------------------------------------- send

def send(conn, *, to: str, brand: str, subject: str, body: str, follow_up: bool = False,
         test_to: str | None = None, user_id: int | None = None) -> dict:
    """Check, log, send. test_to sends the same email to that address instead
    (the admin's own) and doesn't count against the cap or the 14 days.
    Returns the log row; raises Refused (nothing logged, nothing sent)."""
    test = bool(test_to)
    to = norm_email(test_to if test else to)
    subject = " ".join((subject or "").split())[:200]
    brand = (brand or "").strip()[:120]
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")   # the checks and the claim are one step (a double click sends once)
    try:
        check(conn, to=to, subject=subject, body=body, follow_up=follow_up, test=test)
        text, page = compose(body)
        cur = conn.execute("INSERT INTO outreach_log (to_addr, brand, subject, body_text, follow_up, is_test, status, "
                           "user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (to, brand, subject, text, int(follow_up), int(test), "sending", user_id, db.now()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    lid = cur.lastrowid
    key = "outreach-" + hashlib.sha256(f"{lid}|{to}|{subject}".encode()).hexdigest()[:40]
    try:
        mid = mailer.send(frm=from_header(), to=to, subject=subject, html=page, text=text, reply_to=reply_to(),
                          idempotency_key=key, tags={"kind": "outreach"})
        conn.execute("UPDATE outreach_log SET status='sent', provider_id=?, sent_at=? WHERE id=?", (mid, db.now(), lid))
    except Exception as e:  # noqa: BLE001 — a MailError, or anything else: logged, never left "sending"
        conn.execute("UPDATE outreach_log SET status='failed', error=? WHERE id=?", (str(e)[:300] or type(e).__name__, lid))
    conn.commit()
    return conn.execute("SELECT * FROM outreach_log WHERE id=?", (lid,)).fetchone()


def suppress(conn, entry: str, note: str = "", user_id: int | None = None) -> str:
    entry = norm_entry(entry)
    if not valid_entry(entry):
        raise Refused("Add an email address (name@brand.com) or a domain (brand.com).")
    conn.execute("INSERT OR IGNORE INTO outreach_suppressions (entry, note, user_id, created_at) VALUES (?,?,?,?)",
                 (entry, (note or "").strip()[:200], user_id, db.now()))
    conn.commit()
    return entry


def recent(conn, n: int = 50):
    return conn.execute("SELECT * FROM outreach_log ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def suppressions(conn):
    return conn.execute("SELECT * FROM outreach_suppressions ORDER BY created_at DESC, entry").fetchall()

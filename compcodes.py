"""Single-use comp codes: a free month (or a few) of a paid plan with no card.

Admins make codes at /admin/codes (one per business, each with a label such as
"Acme · sam@acme.com"). A workspace enters one at sign-up or on Billing and
gets that plan's features and monthly credits for N months. Nothing touches
Stripe: no customer, no card, no subscription, so there is nothing to charge or
renew. When the months are up the workspace drops back to free on its own
(plans.plan_code stops counting the comp at comp_until; settle() empties the
plan credits).

Rules, all checked in redeem():
- one code per workspace, ever (a unique index on comp_codes.workspace_id);
- a code works once, not after its redeem-by date, and not once revoked;
- a workspace with a live paid subscription is told to keep its plan instead;
- a workspace already on that plan or a higher one (an admin comp) is told so.

Workspace columns: comp_plan, comp_until (ISO UTC; the comp is on while now <
comp_until) and comp_next_grant (when the next month's credits land, for codes
of more than one month). Every redemption is kept on the code row (when, which
workspace, which user) and in the credits ledger.
"""
import calendar
import csv
import io
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import db
import plans

log = logging.getLogger("campaignify.compcodes")

# No 0/O, 1/I/L: easy to read out and type from an email.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PREFIX = "CFY"
MAX_CODES = 200          # per batch
MAX_MONTHS = 12
DEFAULT_REDEEM_DAYS = 30


class Refused(ValueError):
    """Why a code can't be used here (shown to the person as is)."""


# ---------------------------------------------------------------- time helpers

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def add_months(dt: datetime, n: int) -> datetime:
    """Same day n calendar months later (the 31st becomes the month's last day)."""
    m = dt.month - 1 + n
    y, m = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


# ---------------------------------------------------------------- codes

def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{PREFIX}-{raw[:4]}-{raw[4:]}"


def normalize(s: str) -> str:
    """What someone typed -> CFY-XXXX-XXXX (case, spaces and dashes don't matter;
    a typed 0/O or 1/I/L can't be in a real code, so they simply won't match)."""
    s = re.sub(r"[^A-Za-z0-9]", "", s or "").upper()
    if s.startswith(PREFIX) and len(s) == len(PREFIX) + 8:
        s = s[len(PREFIX):]
    if len(s) != 8:
        return ""
    return f"{PREFIX}-{s[:4]}-{s[4:]}"


def parse_redeem_by(s: str | None) -> datetime:
    """A YYYY-MM-DD date (the end of that day, UTC), or DEFAULT_REDEEM_DAYS from now."""
    s = (s or "").strip()
    if not s:
        return _now() + timedelta(days=DEFAULT_REDEEM_DAYS)
    try:
        d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise Refused("Use a redeem-by date like 2026-12-31.") from None
    end = d.replace(hour=23, minute=59, second=59)
    if end <= _now():
        raise Refused("The redeem-by date has to be in the future.")
    return end


def generate(conn, *, plan: str = "growth", months: int = 1, labels=None, count: int = 0,
             redeem_by: str | None = None, user_id=None) -> tuple[str, list]:
    """Make one code per label (or `count` unlabelled codes). Returns (batch id, rows)."""
    if plan not in plans.PLANS or plan == "free":
        raise Refused("Pick a paid plan.")
    try:
        months = int(months)
    except (TypeError, ValueError):
        raise Refused("Months has to be a number.") from None
    if not 1 <= months <= MAX_MONTHS:
        raise Refused(f"Months has to be 1 to {MAX_MONTHS}.")
    labels = [ln.strip()[:200] for ln in (labels or []) if ln and ln.strip()]
    if not labels:
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0
        labels = [""] * count
    if not labels:
        raise Refused("Add a label per line, or a number of codes.")
    if len(labels) > MAX_CODES:
        raise Refused(f"At most {MAX_CODES} codes at a time.")
    until = iso(parse_redeem_by(redeem_by))
    batch = _now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)
    t = db.now()
    ids = []
    for label in labels:
        for _ in range(10):
            code = new_code()
            try:
                ids.append(conn.execute(
                    "INSERT INTO comp_codes (code, label, plan, months, created_at, created_by, redeem_by, batch) "
                    "VALUES (?,?,?,?,?,?,?,?)", (code, label or None, plan, months, t, user_id, until, batch)).lastrowid)
                break
            except db.sqlite3.IntegrityError:
                continue
    conn.commit()
    log.info("admin %s made %s comp code(s) for %s x%s month(s), batch %s", user_id, len(ids), plan, months, batch)
    return batch, conn.execute("SELECT * FROM comp_codes WHERE batch=? ORDER BY id", (batch,)).fetchall()


def status(row, now: str | None = None) -> str:
    """redeemed | revoked | expired | open"""
    now = now or db.now()
    if row["redeemed_at"]:
        return "redeemed"
    if row["revoked"]:
        return "revoked"
    if row["redeem_by"] and row["redeem_by"] < now:
        return "expired"
    return "open"


def listing(conn, limit: int = 500):
    return conn.execute("SELECT c.*, w.name ws_name, u.email user_email FROM comp_codes c "
                        "LEFT JOIN workspaces w ON w.id=c.workspace_id LEFT JOIN users u ON u.id=c.redeemed_by "
                        "ORDER BY c.id DESC LIMIT ?", (limit,)).fetchall()


def revoke(conn, code_id: int, user_id=None) -> bool:
    """Stop an unused code from working. A redeemed code stays as it is."""
    cur = conn.execute("UPDATE comp_codes SET revoked=1, revoked_at=? WHERE id=? AND redeemed_at IS NULL AND revoked=0",
                       (db.now(), code_id))
    conn.commit()
    if cur.rowcount:
        log.info("admin %s revoked comp code %s", user_id, code_id)
    return cur.rowcount == 1


def to_csv(rows) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["code", "label", "plan", "months", "redeem_by", "status", "redeemed_at", "workspace_id"])
    now = db.now()
    for r in rows:
        w.writerow([r["code"], r["label"] or "", r["plan"], r["months"], (r["redeem_by"] or "")[:10], status(r, now),
                    r["redeemed_at"] or "", r["workspace_id"] or ""])
    return out.getvalue()


# ---------------------------------------------------------------- redeeming

def active(ws) -> bool:
    """The workspace is on a comp plan right now."""
    keys = ws.keys()
    return bool("comp_plan" in keys and ws["comp_plan"] in plans.PLANS and ws["comp_until"]
                and ws["comp_until"] > db.now())


def used_code(conn, ws_id: int):
    return conn.execute("SELECT * FROM comp_codes WHERE workspace_id=?", (ws_id,)).fetchone()


def _subscribed(ws) -> bool:
    return bool(ws["stripe_subscription_id"]) and ws["plan_status"] in plans.LIVE_STATUSES


def redeem(conn, ws_id: int, user_id, typed: str, commit: bool = True):
    """Use a code on a workspace: the plan and its first month's credits now.
    Writes nothing on refusal; commits on success (unless commit=False, for
    sign-up's own transaction). Returns the code row. Raises Refused."""
    code = normalize(typed)
    if not code:
        raise Refused("That code doesn't look right. It looks like CFY-ABCD-EFGH.")
    row = conn.execute("SELECT * FROM comp_codes WHERE code=?", (code,)).fetchone()
    if not row:
        raise Refused("We don't know that code. Check it against the email we sent.")
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if ws is None:
        raise Refused("That workspace doesn't exist.")
    if row["redeemed_at"]:
        raise Refused("That code has already been used." if row["workspace_id"] != ws_id
                      else "This workspace already used that code.")
    if row["revoked"]:
        raise Refused("That code has been switched off. Reply to our email and we'll sort it out.")
    if row["redeem_by"] and row["redeem_by"] < db.now():
        raise Refused(f"That code expired on {row['redeem_by'][:10]}. Reply to our email and we'll sort it out.")
    if used_code(conn, ws_id):
        raise Refused("This workspace has already used a free-month code. It's one code per workspace.")
    p = plans.PLANS[row["plan"]]
    if _subscribed(ws):
        raise Refused(f"This workspace already pays for a plan, so the code isn't needed here. Your "
                      f"{plans.plan(ws)['name']} plan and its credits stay as they are.")
    if plans.rank(plans.plan_code(ws)) >= plans.rank(p["code"]):
        raise Refused(f"This workspace is already on {plans.plan(ws)['name']}, so the code isn't needed here.")
    start = _now()
    until = add_months(start, int(row["months"]))
    nxt = add_months(start, 1)
    try:
        cur = conn.execute("UPDATE comp_codes SET redeemed_at=?, workspace_id=?, redeemed_by=?, comp_until=? "
                           "WHERE id=? AND redeemed_at IS NULL AND revoked=0",
                           (iso(start), ws_id, user_id, iso(until), row["id"]))
    except db.sqlite3.IntegrityError:   # another code landed on this workspace at the same moment
        raise Refused("This workspace has already used a free-month code. It's one code per workspace.") from None
    if cur.rowcount != 1:
        raise Refused("That code has already been used.")
    conn.execute("UPDATE workspaces SET comp_plan=?, comp_until=?, comp_next_grant=? WHERE id=?",
                 (p["code"], iso(until), iso(nxt) if nxt < until else None, ws_id))
    db.set_plan_credits(conn, ws_id, p["credits"], f"{p['name']} plan: free month (code {code})")
    if commit:
        conn.commit()
    log.info("comp code %s redeemed: workspace %s, user %s, %s until %s", code, ws_id, user_id, p["code"], iso(until))
    return conn.execute("SELECT * FROM comp_codes WHERE id=?", (row["id"],)).fetchone()


def free_until_text(until_iso: str) -> str:
    """The date a comp ends, as people read it: "December 4, 2026" (UTC)."""
    d = parse(until_iso)
    return f"{d:%B} {d.day}, {d.year}" if d else ""


def applied_message(row) -> str:
    p = plans.PLANS[row["plan"]]
    return (f"Code applied. You're on {p['name']}, free until {free_until_text(row['comp_until'])}, "
            f"with {p['credits']:,} credits a month. No card needed, and it won't renew.")


def due(ws) -> bool:
    """Something to do for this workspace: a month's credits or the end of the comp."""
    keys = ws.keys()
    if "comp_plan" not in keys or not ws["comp_plan"]:
        return False
    now = db.now()
    return bool((ws["comp_until"] and ws["comp_until"] <= now) or (ws["comp_next_grant"] and ws["comp_next_grant"] <= now))


def settle(conn, ws_id: int) -> str | None:
    """Apply what's due: the next month's credits, or the end of the free months
    (back to free: the plan credits go; pack credits stay). A live paid
    subscription keeps its own plan credits. Returns "granted", "ended" or None."""
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if ws is None or not due(ws):
        return None
    now = db.now()
    p = plans.PLANS.get(ws["comp_plan"])
    if not p or ws["comp_until"] <= now:
        cur = conn.execute("UPDATE workspaces SET comp_plan=NULL, comp_until=NULL, comp_next_grant=NULL "
                           "WHERE id=? AND comp_plan IS NOT NULL", (ws_id,))
        if cur.rowcount and not _subscribed(ws) and plans.plan_code(
                conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()) == "free":
            _expire_plan_credits(conn, ws_id, f"{p['name'] if p else 'Plan'} free months ended: unused plan credits expired")
        conn.commit()
        log.info("comp plan ended for workspace %s", ws_id)
        return "ended"
    nxt = parse(ws["comp_next_grant"])
    following = add_months(nxt, 1)
    while iso(following) <= now:      # catching up after downtime: one grant, not one per missed month
        following = add_months(following, 1)
    cur = conn.execute("UPDATE workspaces SET comp_next_grant=? WHERE id=? AND comp_next_grant=?",
                       (iso(following) if iso(following) < ws["comp_until"] else None, ws_id, ws["comp_next_grant"]))
    if cur.rowcount and not _subscribed(ws):
        db.set_plan_credits(conn, ws_id, p["credits"], f"{p['name']} plan: free month credits")
    conn.commit()
    return "granted"


def _expire_plan_credits(conn, ws_id: int, reason: str):
    left = int(conn.execute("SELECT plan_credits FROM workspaces WHERE id=?", (ws_id,)).fetchone()[0] or 0)
    if left > 0:
        conn.execute("UPDATE workspaces SET plan_credits=0 WHERE id=?", (ws_id,))
        conn.execute("INSERT INTO ledger (workspace_id, delta, reason, created_at, plan_delta) VALUES (?,?,?,?,?)",
                     (ws_id, -left, reason, db.now(), -left))


def settle_all(conn) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM workspaces WHERE comp_plan IS NOT NULL AND "
                                         "(comp_until <= ? OR comp_next_grant <= ?)", (db.now(), db.now()))]
    n = 0
    for ws_id in ids:
        try:
            n += settle(conn, ws_id) is not None
        except Exception:  # noqa: BLE001
            log.exception("comp settle failed for workspace %s", ws_id)
    return n

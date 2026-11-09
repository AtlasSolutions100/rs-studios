"""The weekly report email per workspace (roadmap M4.4): spend, results, the best ad and what to change, in plain words.

Once a week (Monday from REPORT_HOUR in the workspace's time zone: workspaces.timezone, else its Meta ad account's,
else America/Chicago; the account-mail loop in nudges.py calls run_once every TICK_PASSES minutes) Campaignify sends each workspace owner one email about the 7 days that just ended:

    * what was spent on the campaigns Campaignify launched, against the week before, per currency;
    * the results as Meta counts them (purchases, leads, link clicks: the campaign's goal), and cost per result;
    * leads on the member's own Campaignify forms and what each cost (adleads.py, M4.1);
    * the best ad: the lowest cost per result with at least 1 result, else the best click rate;
    * what to change: ads wearing out (adfatigue.py, M4.2) with a link to refresh them, campaigns that spent with no
      result, and "nothing this week" when nothing needs doing.

Only when there's something to report: no email for a week with no spend and no form lead. Nothing is promised:
the copy states what happened and what to try.

Nurture class (mailer.py, kind "weekly_report"): it waits while mailer.nurture_block() says no (quota hold, codes
waiting to be resent, EMAIL_NURTURE_DAILY_MAX, the transactional reserve) and tries again on a later pass of the
same week. Each email carries a one-click unsubscribe (List-Unsubscribe and List-Unsubscribe-Post, and a link in
the footer: /u/weekly/<token>, no login), which sets users.weekly_report_off; the Ad campaigns page turns it back
on. One row per workspace, person and week in weekly_reports, written before the send, so a failing mailer never
mails anyone twice.

Behind AD_LAUNCH like the campaigns it reports on. WEEKLY_REPORT=0 turns the emails off.
"""
import html as htmlmod
import logging
import os
import time
from datetime import date, datetime, timedelta

import db

log = logging.getLogger("campaignify.weekly")

KIND = "weekly_report"
REPORT_HOUR = 8                 # local time on Monday
SEND_UNTIL_WEEKDAY = 2          # a week's report still goes out until Wednesday (nurture may wait a day)
TICK_PASSES = 15                # nudges.mail_tick runs every minute: a pass every 15 minutes
DEFAULT_TZ = "America/Chicago"
SALT = "cfy-weekly-report-unsub"
CLOCK = time.time               # tests move it


def enabled() -> bool:
    return (os.environ.get("WEEKLY_REPORT") or "1").strip().lower() not in ("0", "off", "false", "no")


# ---------------------------------------------------------------- when

def _tz(conn, ws):
    from zoneinfo import ZoneInfo
    name = (ws["timezone"] if "timezone" in ws.keys() else None) or None
    if not name:
        r = conn.execute("SELECT timezone FROM ad_connections WHERE workspace_id=? AND platform='meta' AND timezone IS NOT "
                         "NULL ORDER BY id DESC LIMIT 1", (ws["id"],)).fetchone()
        name = r["timezone"] if r else None
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_TZ)


def week_of(local_now: datetime) -> date | None:
    """The Monday that starts the report week now due, or None when no report is due at this local time. A report
    covers the 7 days before that Monday."""
    monday = local_now.date() - timedelta(days=local_now.weekday())
    if local_now.weekday() == 0 and local_now.hour < REPORT_HOUR:
        return None
    if local_now.weekday() > SEND_UNTIL_WEEKDAY:
        return None
    return monday


# ---------------------------------------------------------------- what it says

def _money(minor, cur):
    import launches
    return launches.money(int(minor), cur)


ONE = {"leads": "lead", "purchases": "purchase", "link clicks": "link click", "people reached": "person reached",
       "sign-ups": "sign-up", "adds to cart": "add to cart", "checkouts started": "checkout started",
       "content views": "content view", "contacts": "contact", "bookings": "booking", "applications": "application",
       "results": "result"}


def _n(n, plural: str) -> str:
    """"1 lead", "4 leads": Meta's result words (adinsights.RESULT_WORDS) with the count."""
    p = plural.lower()
    return f"{n:,} {ONE.get(p, p) if n == 1 else p}"


def _pct(a, b):
    return round((a - b) * 100.0 / b) if b else None


def build(conn, ws, week_start: date | None = None) -> dict | None:
    """The report for one workspace, or None when there's nothing to report. Every launch's numbers are its ad
    account's days (adinsights.week_over_week's "this" week: the 7 complete days before today)."""
    import adfatigue
    import adinsights
    import adleads
    import admanage
    Ls = conn.execute("SELECT l.*, c.timezone tz, c.currency cur FROM launches l JOIN ad_connections c ON "
                      "c.id=l.connection_id WHERE l.workspace_id=? AND l.platform='meta' AND l.status='paused' AND "
                      "l.external_campaign_id IS NOT NULL ORDER BY l.id", (ws["id"],)).fetchall()
    if not Ls:
        return None
    camps, by_cur, best, todo = [], {}, None, []
    fat = {L["id"]: r for L, r in adfatigue.workspace(conn, ws["id"])}
    if week_start is None:
        t = adinsights.today(Ls[0]["tz"])
        week_start = t - timedelta(days=t.weekday())
    since, until = week_start - timedelta(days=7), week_start - timedelta(days=1)
    s_, u_, ps, pu = since.isoformat(), until.isoformat(), (since - timedelta(days=7)).isoformat(), \
        (since - timedelta(days=1)).isoformat()
    for L in Ls:
        tz = L["tz"]
        f = adinsights._sum(conn, "launch_id=? AND day BETWEEN ? AND ?", (L["id"], s_, u_))
        prev = adinsights._sum(conn, "launch_id=? AND day BETWEEN ? AND ?", (L["id"], ps, pu))["spend"]
        name = admanage.summary(conn, L)["name"]
        cur = f["cur"] or L["cur"] or "USD"
        ad_fig = {r["external_object_id"]: adinsights._sum(
            conn, "launch_id=? AND external_object_id=? AND day BETWEEN ? AND ?", (L["id"], r["external_object_id"], s_, u_))
            for r in conn.execute("SELECT DISTINCT external_object_id FROM ad_insights_daily WHERE launch_id=? AND day "
                                  "BETWEEN ? AND ?", (L["id"], s_, u_)).fetchall()}
        lf = adleads.leads_between(conn, L, since, until, tz, {a: x["spend"] for a, x in ad_fig.items()},
                                   f["spend"] if f["n"] else None)
        if not f["n"] and not lf["total"]:
            continue
        t = by_cur.setdefault(cur, {"spend": 0, "prev": 0, "leads": 0, "cur": cur})
        t["spend"] += f["spend"]
        t["prev"] += prev
        t["leads"] += lf["total"]
        camps.append({"id": L["id"], "name": name, "cur": cur, "fig": f, "leads": lf, "prev": prev})
        r = fat.get(L["id"]) or {"ads": [], "tired": []}
        names = {a["id"]: a for a in r["ads"]}
        for aid, af in ad_fig.items():
            a = names.get(aid)
            if a is None:
                continue
            cand = {"camp": name, "launch_id": L["id"], "n": a["n"], "name": a["name"], "cur": cur, "fig": af,
                    "leads": lf["by_ad"].get(aid, 0), "cpl": lf["ad_cpl"].get(aid)}
            if _better(cand, best):
                best = cand
        for a in r["tired"]:
            todo.append({"kind": "refresh", "camp": name, "launch_id": L["id"], "ad": a})
        if f["n"] and f["spend"] and not f["results"] and not lf["total"]:
            todo.append({"kind": "no_results", "camp": name, "launch_id": L["id"], "spend": f["spend"], "cur": cur,
                         "words": f["words"][0].lower()})
    if not camps or not any(t["spend"] or t["leads"] for t in by_cur.values()):
        return None
    return {"ws": ws, "since": since, "until": until, "camps": camps, "totals": list(by_cur.values()), "best": best,
            "todo": todo, "week": week_start.isoformat()}


def _better(a, b) -> bool:
    """Lower cost per result wins among ads with results; else the higher click rate."""
    if b is None:
        return bool(a["fig"]["n"])
    ar, br = a["fig"]["results"], b["fig"]["results"]
    if ar and br:
        return (a["fig"]["cpr"] or 0) < (b["fig"]["cpr"] or 0)
    if ar or br:
        return bool(ar)
    return (a["fig"]["ctr"] or 0) > (b["fig"]["ctr"] or 0)


def render(rep: dict, base: str, unsub: str) -> tuple:
    """(subject, text, html) in plain words."""
    import adfatigue
    ws = rep["ws"]
    days = f"{_d(rep['since'])} to {_d(rep['until'])}"
    tot = rep["totals"]
    spent = " and ".join(_money(t["spend"], t["cur"]) for t in tot)
    leads = sum(t["leads"] for t in tot)
    subject = f"Your week on Meta: {spent} spent" + (f", {leads:,} lead{'' if leads == 1 else 's'} on your forms" if leads else "")
    lines, parts = [], []

    def para(txt, html_=None):
        lines.append(txt)
        parts.append(f"<p>{html_ or htmlmod.escape(txt)}</p>")

    name = ws["name"] or "your workspace"
    para(f"Here's last week ({days}) for the campaigns Campaignify launched for {name}.")
    for t in tot:
        ch = _pct(t["spend"], t["prev"])
        vs = (f", {abs(ch)}% {'more' if ch > 0 else 'less'} than the week before" if ch else
              (", about the same as the week before" if ch == 0 else ""))
        para(f"Spent: {_money(t['spend'], t['cur'])}{vs}.")
    for c in rep["camps"]:
        f, lf = c["fig"], c["leads"]
        bits = [f"{_money(f['spend'], c['cur'])} spent"]
        if f["results"] is not None:
            bits.append(f"{_n(f['results'], f['words'][0])} as Meta counts them"
                        + (f" at {_money(f['cpr'], c['cur'])} each" if f["cpr"] is not None else ""))
        if lf["total"]:
            bits.append(f"{lf['total']:,} lead{'' if lf['total'] == 1 else 's'} on your forms"
                        + (f" at {_money(lf['cpl'], c['cur'])} each" if lf["cpl"] is not None else ""))
        if f["ctr"] is not None:
            bits.append(f"{f['ctr']:.2f}% click rate")
        para(f"{c['name']}: " + ", ".join(bits) + ".")
    b = rep["best"]
    if b:
        why = (f"{_n(b['fig']['results'], b['fig']['words'][0])} at {_money(b['fig']['cpr'], b['cur'])} each"
               if b["fig"]["results"] and b["fig"]["cpr"] is not None else f"a {b['fig']['ctr']:.2f}% click rate"
               if b["fig"]["ctr"] is not None else "the most delivery")
        para(f"Best ad: Ad {b['n']} {b['name']} in {b['camp']}, with {why}.")
    lines.append("")
    parts.append("<p><b>What to change</b></p>")
    items = []
    for t in rep["todo"]:
        if t["kind"] == "refresh":
            a = t["ad"]
            link = f"{base}/app/create?refresh={t['launch_id']}:{a['id']}"
            items.append((f"Refresh Ad {a['n']} {a['name']} in {t['camp']}. {adfatigue.words(a)} Make a new version: {link}",
                          f"Refresh Ad {a['n']} {htmlmod.escape(a['name'])} in {htmlmod.escape(t['camp'])}. "
                          f"{htmlmod.escape(adfatigue.words(a))} <a href=\"{htmlmod.escape(link)}\">Make a new version</a>"))
        elif t["kind"] == "no_results":
            link = f"{base}/app/ad-campaigns/{t['launch_id']}"
            txt = (f"{t['camp']} spent {_money(t['spend'], t['cur'])} with no {t['words']} and no form leads. Check that "
                   f"the landing page loads and its form or pixel works, or pause it: ")
            items.append((txt + link, htmlmod.escape(txt) + f"<a href=\"{htmlmod.escape(link)}\">open the campaign</a>"))
    if not items:
        items.append(("Nothing needs changing this week. Let the ads keep running.",
                      "Nothing needs changing this week. Let the ads keep running."))
    for txt, h in items:
        lines.append("- " + txt)
    parts.append("<ul>" + "".join(f"<li>{h}</li>" for _, h in items) + "</ul>")
    dash = f"{base}/app/ad-campaigns"
    lines += ["", f"See every number: {dash}", "",
              "Numbers come from Meta in your ad account's currency and time zone, and late purchases can still add up "
              "for a few days. Nothing in this email changes your campaigns.", "",
              f"You get this because you own this Campaignify workspace. Stop these weekly emails: {unsub}", "Campaignify"]
    parts.append(f'<p><a href="{htmlmod.escape(dash)}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
                 'border-radius:8px;text-decoration:none;font-weight:600">See every number</a></p>')
    parts.append('<p style="font-size:13px;color:#555">Numbers come from Meta in your ad account\'s currency and time zone, '
                 "and late purchases can still add up for a few days. Nothing in this email changes your campaigns.</p>")
    parts.append(f'<p style="font-size:13px;color:#555">You get this because you own this Campaignify workspace. '
                 f'<a href="{htmlmod.escape(unsub)}">Stop these weekly emails</a>.</p>')
    html_ = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111;max-width:600px">'
             + "".join(parts) + "</div>")
    return subject, "\n".join(lines) + "\n", html_


def _d(day) -> str:
    return f"{day.strftime('%b')} {day.day}"


# ---------------------------------------------------------------- unsubscribe

def _serializer(secret: str):
    from itsdangerous import URLSafeSerializer
    return URLSafeSerializer(secret, salt=SALT)


def unsub_token(secret: str, user) -> str:
    return _serializer(secret).dumps({"u": int(user["id"])})


def user_of_token(conn, secret: str, token: str):
    from itsdangerous import BadSignature
    try:
        uid = int(_serializer(secret).loads(token or "")["u"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def set_off(conn, user_id: int, off: bool) -> None:
    conn.execute("UPDATE users SET weekly_report_off=? WHERE id=?", (db.now() if off else None, int(user_id)))
    conn.commit()


# ---------------------------------------------------------------- sending

def recipients(conn, ws) -> list:
    return conn.execute("SELECT * FROM users WHERE workspace_id=? AND role='owner' AND email_verified_at IS NOT NULL AND "
                        "weekly_report_off IS NULL ORDER BY id", (ws["id"],)).fetchall()


def send_one(conn, secret: str, ws, user, rep: dict) -> str:
    """sent | skipped (the mailer said no) | failed. The row is written before the send (never twice)."""
    import mailer
    import verify
    base = verify.public_url()
    unsub = f"{base}/u/weekly/{unsub_token(secret, user)}"
    subject, text, html_ = render(rep, base, unsub)
    cur = conn.execute("INSERT OR IGNORE INTO weekly_reports (workspace_id, user_id, week, status, subject, created_at) "
                       "VALUES (?,?,?,?,?,?)", (ws["id"], user["id"], rep["week"], "sending", subject[:200], db.now()))
    conn.commit()
    if cur.rowcount != 1:
        return "skipped"
    try:
        mailer.send(frm=verify.from_address(), to=user["email"], subject=subject, html=html_, text=text,
                    reply_to=verify.reply_to(), tags={"kind": KIND},
                    headers={"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
                    idempotency_key=f"weekly-{ws['id']}-{user['id']}-{rep['week']}")
        conn.execute("UPDATE weekly_reports SET status='sent', sent_at=? WHERE workspace_id=? AND user_id=? AND week=?",
                     (db.now(), ws["id"], user["id"], rep["week"]))
        conn.commit()
        return "sent"
    except mailer.MailError as e:
        nurture_wait = str(e).startswith("Not sent:") or isinstance(e, mailer.MailDeferred)
        if nurture_wait:          # the nurture budget said wait: free the row so a later pass this week tries again
            conn.execute("DELETE FROM weekly_reports WHERE workspace_id=? AND user_id=? AND week=?",
                         (ws["id"], user["id"], rep["week"]))
        else:
            conn.execute("UPDATE weekly_reports SET status='failed', error=? WHERE workspace_id=? AND user_id=? AND week=?",
                         (str(e)[:300], ws["id"], user["id"], rep["week"]))
        conn.commit()
        log.warning("weekly report for workspace %s not sent: %s", ws["id"], str(e)[:150])
        return "skipped" if nurture_wait else "failed"


def run_once(secret: str, now: float | None = None) -> int:
    """One pass: every workspace with launched Meta campaigns and AD_LAUNCH whose report is due. Returns emails sent."""
    import adplatforms
    import mailer
    if not enabled() or not mailer.configured():
        return 0
    t = now if now is not None else CLOCK()
    sent = 0
    with db.connect() as conn:
        wss = conn.execute("SELECT DISTINCT w.* FROM workspaces w JOIN launches l ON l.workspace_id=w.id WHERE "
                           "l.platform='meta' AND l.status='paused'").fetchall()
        for ws in wss:
            if not adplatforms.enabled(ws):
                continue
            week = week_of(datetime.fromtimestamp(t, _tz(conn, ws)))
            if week is None:
                continue
            people = [u for u in recipients(conn, ws) if not conn.execute(
                "SELECT 1 FROM weekly_reports WHERE workspace_id=? AND user_id=? AND week=?",
                (ws["id"], u["id"], week.isoformat())).fetchone()]
            if not people:
                continue
            if mailer.nurture_block(conn):
                break                   # the nurture budget is spent for now; a later pass tries again
            rep = build(conn, ws, week)
            if rep is None:            # nothing to report this week: noted, so later passes don't look again
                for u in people:
                    conn.execute("INSERT OR IGNORE INTO weekly_reports (workspace_id, user_id, week, status, created_at) "
                                 "VALUES (?,?,?,?,?)", (ws["id"], u["id"], week.isoformat(), "nothing", db.now()))
                conn.commit()
                continue
            for u in people:
                if send_one(conn, secret, ws, u, rep) == "sent":
                    sent += 1
    return sent

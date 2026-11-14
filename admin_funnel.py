"""Admin read-outs for the ad funnel: recent sign-ups (masked) and a small
system/health card (generation provider, vendor-price check, pixels).

Read-only; the routes that show them are admin-only (app.admin, app.admin_signups).
Emails are masked here ("ko•••@gmail.com"); nothing else personal is shown.
"""
import json
from datetime import datetime, timedelta, timezone

import adoffer
import gads
import hf
import pixels
import verify

STARTER_REASONS = ("starter credits", adoffer.REASON)
CLICK_NAMES = {"gclid": "Google", "gbraid": "Google", "wbraid": "Google", "fbclid": "Meta", "ttclid": "TikTok",
               "rdt_cid": "Reddit", "twclid": "X"}


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "•••"
    keep = 1 if len(local) <= 3 else 2
    return f"{local[:keep]}•••@{domain}"


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def source(first_touch: str | None) -> dict:
    """{"label", "paid", "detail"} for a stored first touch (users.first_touch JSON)."""
    try:
        t = json.loads(first_touch) if first_touch else {}
    except (ValueError, TypeError):
        t = {}
    if not isinstance(t, dict) or not t:
        return {"label": "direct / unknown", "paid": False, "detail": ""}
    clicks = sorted({CLICK_NAMES[k] for k in CLICK_NAMES if t.get(k)})
    src = t.get("utm_source") or (clicks[0].lower() if clicks else "") or t.get("ref") or "unknown"
    med = t.get("utm_medium") or ("cpc" if clicks else "")
    label = f"{src} / {med}" if med else src
    detail = " · ".join(x for x in (t.get("utm_campaign") and f"campaign {t['utm_campaign']}",
                                   clicks and f"{', '.join(clicks)} click id",
                                   t.get("lp") and f"landed {t['lp']}") if x)
    return {"label": label, "paid": adoffer.paid_source(t), "detail": detail}


def signups(conn, days: int = 30, limit: int = 500) -> dict:
    """Sign-ups in the last `days` days, newest first, plus funnel totals."""
    users = conn.execute("SELECT u.id, u.email, u.created_at, u.email_verified_at, u.first_touch, u.is_admin, u.role, "
                         "u.workspace_id FROM users u WHERE u.created_at >= ? ORDER BY u.id DESC LIMIT ?",
                         (_since(days), limit)).fetchall()
    rows = []
    for u in users:
        first = conn.execute("SELECT MIN(id) FROM users WHERE workspace_id=?", (u["workspace_id"],)).fetchone()[0]
        grants = conn.execute("SELECT reason, COALESCE(SUM(delta),0) n FROM ledger WHERE workspace_id=? AND delta>0 "
                              f"AND reason IN ({','.join('?' * len(STARTER_REASONS))}) GROUP BY reason",
                              (u["workspace_id"], *STARTER_REASONS)).fetchall() if first == u["id"] else []
        g = {r["reason"]: r["n"] for r in grants}
        jobs = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(status='done'),0) done FROM jobs WHERE user_id=?",
                            (u["id"],)).fetchone()
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()
        rows.append({"id": u["id"], "ws": u["workspace_id"], "email": mask_email(u["email"]), "created_at": u["created_at"],
                     "verified": bool(u["email_verified_at"]), "admin": bool(u["is_admin"]),
                     "joined": first != u["id"], "source": source(u["first_touch"]),
                     "starter": sum(g.values()), "ad_grant": g.get(adoffer.REASON, 0),
                     "jobs": jobs["n"], "done": jobs["done"],
                     "paying": bool(ws is not None and first == u["id"] and verify.paid(conn, ws))})
    people = [r for r in rows if not r["admin"]]
    totals = {"signups": len(people), "verified": sum(r["verified"] for r in people),
              "paid_source": sum(r["source"]["paid"] for r in people),
              "ad_grant": sum(1 for r in people if r["ad_grant"]),
              "made": sum(1 for r in people if r["jobs"]), "finished": sum(1 for r in people if r["done"]),
              "paying": sum(r["paying"] for r in people)}
    return {"rows": rows, "totals": totals, "days": days}


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(row["value"]) if row else None
    except (ValueError, TypeError):
        return None


IMPORT_METHODS = ("shopify-json", "jsonld", "og", "page", "manual")


def imports(conn, days: int = 7) -> dict:
    """"Import from a link" over the last `days` days: tries, outcomes, how each
    product was read (page / jsonld / og / shopify-json / manual) and the stores
    that refused us most."""
    since = _since(days)
    rows = conn.execute("SELECT method, status, COUNT(*) n FROM import_attempts WHERE created_at >= ? "
                        "GROUP BY method, status", (since,)).fetchall()
    by = {}
    status = {"ok": 0, "blocked": 0, "failed": 0}
    for r in rows:
        if r["method"] == "manual":
            continue
        status[r["status"]] = status.get(r["status"], 0) + r["n"]
        if r["status"] == "ok":
            by[r["method"]] = by.get(r["method"], 0) + r["n"]
    manual = sum(r["n"] for r in rows if r["method"] == "manual")
    blocked = conn.execute("SELECT domain, COUNT(*) n FROM import_attempts WHERE created_at >= ? AND status='blocked' "
                           "GROUP BY domain ORDER BY n DESC, domain LIMIT 5", (since,)).fetchall()
    return {"days": days, "tries": sum(status.values()), "ok": status["ok"], "blocked": status["blocked"],
            "failed": status["failed"], "manual": manual,
            "methods": [(m, by[m]) for m in IMPORT_METHODS if by.get(m)] + [(m, n) for m, n in by.items()
                                                                          if m not in IMPORT_METHODS],
            "blocked_domains": [(r["domain"], r["n"]) for r in blocked]}


def health(conn) -> dict:
    """The admin system card: provider, vendor-price check, pixels, last 24h of jobs."""
    import worker
    probe = hf._probe.get("result") if hf.configured() else "unconfigured"
    day = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(status='done'),0) done, COALESCE(SUM(status='failed'),0) failed, "
                       "COALESCE(SUM(status IN ('pending','running')),0) open FROM jobs WHERE created_at >= ?",
                       (_since(1),)).fetchone()
    last_fail = conn.execute("SELECT id, error, updated_at FROM jobs WHERE status='failed' "
                             "ORDER BY id DESC LIMIT 1").fetchone()
    check = _meta(conn, "vendor_prices") or {}
    rows = check.get("rows") or []
    live = _meta(conn, worker.ESTIMATE_META) or {}
    px = pixels.ids()
    google = {"tag": gads.tag_id(), "sign_up": bool(gads.label("sign_up")), "purchase": bool(gads.label("purchase"))}
    try:
        import conversions
        server = conversions.enabled_platforms()
    except Exception:  # noqa: BLE001
        server = []
    warn = []
    if rows and not any(r.get("vendor") is not None for r in rows):
        warn.append("The last vendor-price check got no readable price for any format. Generation isn't blocked: "
                    "talking videos start at our table price.")
    if live and live.get("ok") is False:
        warn.append(f"The last {live.get('misses') or 1} talking-video estimate(s) were unreadable: they started at "
                    "our table price (not blocked).")
    return {"worker": _worker_status(conn),       # where the background loop runs (M7.2)
            "configured": hf.configured(), "probe": probe or "not checked yet",
            "jobs_24h": dict(day), "last_fail": dict(last_fail) if last_fail else None,
            "check_at": check.get("at"), "check_answered": sum(r.get("vendor") is not None for r in rows),
            "check_total": len(rows), "live": live, "tolerance": worker.PRICE_TOLERANCE,
            "pixels": [("Google", bool(google["tag"]), google["tag"] + (" · sign-up label" if google["sign_up"] else
                                                                          " · no sign-up label")
                        + (" · purchase label" if google["purchase"] else " · no purchase label") if google["tag"] else ""),
                       ("Meta", bool(px["meta"]), px["meta"]), ("TikTok", bool(px["tiktok"]), px["tiktok"]),
                       ("Reddit", bool(px["reddit"]), px["reddit"]), ("X", bool(px["x"]), px["x"])],
            "server_side": server, "warn": warn, "imports": imports(conn), "backup": _backup_status(),
            "mail": _mail_status(conn), "dbload": _dbload(conn)}


def _dbload(conn) -> dict | None:
    """The Database load row (dbstats.report, roadmap M7.3). None when it can't be read."""
    try:
        import dbstats
        return dbstats.report(conn, hours=48)
    except Exception:  # noqa: BLE001
        return None


def _worker_status(conn) -> dict:
    """Where the background loop runs and the queued AI runs (roadmap M7.2)."""
    import alerts
    import tasks
    import worker
    out = {"mode": worker.mode(), "state": {}, "tasks": {}}
    try:
        out["state"] = alerts.worker_state(conn)
        out["tasks"] = tasks.counts(conn)
        lease = conn.execute("SELECT holder, until FROM worker_leases WHERE name='worker'").fetchone()
        out["lease"] = dict(lease) if lease else None
        import workerproc
        out["stall"] = workerproc.last_stall(conn)       # the last time the watchdog had to restart a hung loop
    except Exception:  # noqa: BLE001
        pass
    return out


def _mail_status(conn) -> dict:
    try:
        import mailer
        return mailer.status(conn)
    except Exception:  # noqa: BLE001 — the card must render even if this breaks
        return {"configured": False}


def _backup_status() -> dict:
    try:
        import backup
        return backup.status()
    except Exception:  # noqa: BLE001 — the card must render even if this breaks
        return {"configured": False, "missing": [], "last": None, "good": None, "error": "status unreadable"}


# ---------------------------------------------------------------- where sign-ups stop (site_events.on_onboard)
ONBOARD_SINCE = "2026-11-14T08:00:00+00:00"     # step tracking began with this release; older sign-ups have no steps


def _journey(conn, ws_id: int, verified: bool) -> dict:
    """What one new workspace did at each step of the sign-up flow, and where it stopped (the furthest step it
    opened; a confirmed email counts as reaching that step, as Google sign-ins never see it)."""
    import site_events
    rows = conn.execute("SELECT name, detail FROM site_events WHERE workspace_id=? AND name IN "
                        "('onboard_step', 'verify_completed', 'checkout', 'paid', 'first_ad')", (ws_id,)).fetchall()
    seen = {(r["detail"] or "") for r in rows if r["name"] == "onboard_step"}
    names = {r["name"] for r in rows}
    st = {}
    for key, _label in site_events.ONBOARD_STEPS:
        acts = {a for a in site_events.ONBOARD_ACTIONS if f"{key}:{a}" in seen}
        st[key] = acts
    if verified or "verify_completed" in names:
        st["confirm"] |= {"done"}
    if "checkout" in names:
        st["plan"] |= {"checkout"}
    if "paid" in names:
        st["plan"] |= {"paid"}
    if "first_ad" in names:
        st["first"] |= {"made_ad"}
    # the furthest step it reached, and what happened there
    last, state = None, ""
    for key, label in site_events.ONBOARD_STEPS:
        a = st[key]
        if not a or (key == "confirm" and "seen" not in a):    # a Google sign-in is confirmed without the page
            continue
        last = (key, label)
        if key == "plan":
            state = ("paid" if "paid" in a else "left Stripe checkout" if "checkout" in a and "checkout_canceled" not in a
                     else "canceled at Stripe" if "checkout_canceled" in a else "skipped" if "skipped" in a
                     else "saw plans, didn't pick")
        else:
            state = "skipped" if "skipped" in a else "done" if a & {"done", "made_ad"} else "opened, didn't finish"
    return {"steps": st, "last": last, "state": state}


def steps(conn, days: int = 30) -> dict:
    """Sign-ups since step tracking began (owners' workspaces only, admins left out): how many reached, finished and
    skipped each step, and how many stopped there."""
    import site_events
    since = max(_since(days), ONBOARD_SINCE)
    users = conn.execute("SELECT u.id, u.workspace_id, u.email_verified_at FROM users u WHERE u.created_at >= ? AND "
                         "u.is_admin=0 AND u.id=(SELECT MIN(id) FROM users x WHERE x.workspace_id=u.workspace_id)",
                         (since,)).fetchall()
    table = {k: {"key": k, "label": lbl, "reached": 0, "done": 0, "skipped": 0, "stopped": 0}
             for k, lbl in site_events.ONBOARD_STEPS}
    plan = {"checkout": 0, "canceled": 0, "left_checkout": 0, "paid": 0}
    nowhere, per_ws = 0, {}
    for u in users:
        j = _journey(conn, u["workspace_id"], bool(u["email_verified_at"]))
        per_ws[u["workspace_id"]] = j
        for k, acts in j["steps"].items():
            if acts:
                table[k]["reached"] += 1
            if acts & {"done", "made_ad", "paid"}:
                table[k]["done"] += 1
            if "skipped" in acts:
                table[k]["skipped"] += 1
        p = j["steps"]["plan"]
        plan["checkout"] += "checkout" in p
        plan["canceled"] += "checkout_canceled" in p
        plan["paid"] += "paid" in p
        plan["left_checkout"] += "checkout" in p and "paid" not in p and "checkout_canceled" not in p
        if j["last"] is None:
            nowhere += 1
        elif not (j["last"][0] == "first" and j["steps"]["first"] & {"done", "made_ad"}):
            table[j["last"][0]]["stopped"] += 1
    return {"since": since, "signups": len(users), "steps": list(table.values()), "plan": plan,
            "no_steps": nowhere, "per_ws": per_ws}

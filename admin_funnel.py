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
        rows.append({"id": u["id"], "email": mask_email(u["email"]), "created_at": u["created_at"],
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
    return {"configured": hf.configured(), "probe": probe or "not checked yet",
            "jobs_24h": dict(day), "last_fail": dict(last_fail) if last_fail else None,
            "check_at": check.get("at"), "check_answered": sum(r.get("vendor") is not None for r in rows),
            "check_total": len(rows), "live": live, "tolerance": worker.PRICE_TOLERANCE,
            "pixels": [("Google", bool(google["tag"]), google["tag"] + (" · sign-up label" if google["sign_up"] else
                                                                          " · no sign-up label")
                        + (" · purchase label" if google["purchase"] else " · no purchase label") if google["tag"] else ""),
                       ("Meta", bool(px["meta"]), px["meta"]), ("TikTok", bool(px["tiktok"]), px["tiktok"]),
                       ("Reddit", bool(px["reddit"]), px["reddit"]), ("X", bool(px["x"]), px["x"])],
            "server_side": server, "warn": warn, "imports": imports(conn)}

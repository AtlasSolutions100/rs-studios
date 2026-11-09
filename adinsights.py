"""Results of launched campaigns: Meta insights pulled into ad_insights_daily (roadmap M2.5, design doc 4.4).

Every 6 hours (AD_INSIGHTS_HOURS changes it) the worker pulls, per connected Meta ad account, today and the 14
complete days before it (WINDOW_DAYS) of per-ad, per-day numbers for the campaigns Campaignify launched there (tick()):

    * one GET /act_<id>/insights (level=ad, time_increment=1, filtered on the launched campaigns, paged), plus one
      multi-id read of the campaigns, ad sets and ads so the page also shows what was changed in Ads Manager;
    * every call goes through adhttp: it spends from the ad account's rate budget and never waits there (max_wait=0).
      When the budget is spent, Meta says slow down (17, 80000 and the rest), or the stored usage headers say the
      account or its insights are throttled (x-business-use-case-usage, x-ad-account-usage,
      x-fb-ads-insights-throttle), the pull moves to later (insights_next_at) instead of failing;
    * Meta's "reduce the amount of data" (code 1) splits the date range in halves, down to single days;
    * 190 marks the connection for Reconnect; other errors are kept in plain words and retried in an hour.

Each row is upserted (platform, ad id, day), so re-pulling the window updates late purchases; a day Meta no longer
reports inside a pulled window is removed. Money is in the account's minor units (adspec.minor_factor), the day is
the account's own date. Rates (click rate, cost per click, cost per result) are computed from sums, never averaged.

"Results" follow the campaign's goal, as Ads Manager counts them: the pixel event it optimizes for (Purchase, Lead and
the other standard events), link clicks for traffic, people reached for awareness. week_over_week() gives M4.2's
creative fatigue check its inputs (link click rate this week against last week, at the spend of each).

Everything sits behind AD_LAUNCH: the worker pulls only for workspaces that have it. Meta launches only (Google
results come with Google's own reporting task).
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import adhttp
import admanage
import adplatforms
import adspec
import db
import secretsbox

log = logging.getLogger("campaignify.insights")

WINDOW_DAYS = 15             # today and the 14 complete days before it: two full weeks for M4.2
RETRY_ERROR_S = 3600
REFRESH_MIN_S = 15 * 60       # "Update now" at most this often per connection
TICK_SECONDS = 60
PER_TICK = 2                  # connections pulled per worker pass
CLOCK = time.time             # tests move it
_last_tick = [0.0]


def every_s() -> float:
    try:
        h = float(os.environ.get("AD_INSIGHTS_HOURS") or 6)
    except ValueError:
        h = 6.0
    return max(1.0, min(24.0, h)) * 3600


# What counts as a result, per conversion event: Meta action types in priority order (the first one present is used,
# so a purchase is never counted twice as offsite_conversion.fb_pixel_purchase and purchase / omni_purchase).
EVENT_ACTIONS = {
    "Purchase": ("offsite_conversion.fb_pixel_purchase", "purchase", "omni_purchase"),
    "Lead": ("offsite_conversion.fb_pixel_lead", "lead"),
    "CompleteRegistration": ("offsite_conversion.fb_pixel_complete_registration", "complete_registration"),
    "AddToCart": ("offsite_conversion.fb_pixel_add_to_cart", "add_to_cart", "omni_add_to_cart"),
    "InitiateCheckout": ("offsite_conversion.fb_pixel_initiate_checkout", "initiate_checkout"),
    "ViewContent": ("offsite_conversion.fb_pixel_view_content", "view_content"),
    "Contact": ("offsite_conversion.fb_pixel_contact", "contact_total", "contact"),
    "Schedule": ("offsite_conversion.fb_pixel_schedule", "schedule_total", "schedule"),
    "SubmitApplication": ("offsite_conversion.fb_pixel_submit_application", "submit_application_total",
                          "submit_application"),
}
PURCHASE = EVENT_ACTIONS["Purchase"]
LEAD = EVENT_ACTIONS["Lead"]
# result_kind -> (what the results are called, what the cost per result is called), for the pages
RESULT_WORDS = {"purchase": ("Purchases", "Cost per purchase"), "lead": ("Leads", "Cost per lead"),
                "link_click": ("Link clicks", "Cost per link click"),
                "reach": ("People reached", "Cost per 1,000 people reached"),
                "complete_registration": ("Sign-ups", "Cost per sign-up"), "add_to_cart": ("Adds to cart", "Cost per add to cart"),
                "initiate_checkout": ("Checkouts started", "Cost per checkout started"),
                "view_content": ("Content views", "Cost per content view"), "contact": ("Contacts", "Cost per contact"),
                "schedule": ("Bookings", "Cost per booking"), "submit_application": ("Applications", "Cost per application")}
EVENT_KIND = {"Purchase": "purchase", "Lead": "lead", "CompleteRegistration": "complete_registration",
              "AddToCart": "add_to_cart", "InitiateCheckout": "initiate_checkout", "ViewContent": "view_content",
              "Contact": "contact", "Schedule": "schedule", "SubmitApplication": "submit_application"}

WORDS = {
    "rate": "Meta asked us to slow down, so the numbers update a little later.",
    "auth": "Meta ended Campaignify's access to this ad account. Reconnect it in Settings > Connections to see results.",
    "permission": "Campaignify is missing Meta's ads_read permission. Reconnect the ad account and allow it.",
    "network": "We couldn't reach Meta, so these are the numbers from the last update.",
    "other": "Meta didn't send the numbers this time. We'll try again within the hour.",
}


# ---------------------------------------------------------------- time

def _now_iso(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(CLOCK() + offset_s, timezone.utc).replace(microsecond=0).isoformat()


def today(tz_name: str | None) -> date:
    """Today in the ad account's time zone (Meta's days are the account's days)."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.fromtimestamp(CLOCK(), tz).date()


# ---------------------------------------------------------------- one row

def _num(v) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else 0))
    except InvalidOperation:
        return Decimal(0)


def _int(v) -> int:
    return int(_num(v).to_integral_value(ROUND_HALF_UP))


def _minor(v, currency: str) -> int:
    return int((_num(v) * adspec.minor_factor(currency)).to_integral_value(ROUND_HALF_UP))


def _action(rows, types) -> tuple:
    """(value, action type) of the first of `types` present in Meta's actions / action_values list."""
    by = {}
    for a in rows or []:
        if isinstance(a, dict) and a.get("action_type"):
            by[a["action_type"]] = a.get("value")
    for t in types:
        if t in by:
            return by[t], t
    return 0, None


def goal_of(spec: dict) -> tuple:
    """(result_kind, action types or None) for a launch spec: what Ads Manager reports as its results."""
    obj = spec.get("objective") or "sales"
    conv = spec.get("conversion") or {}
    if obj in ("sales", "leads") and conv.get("pixel_id"):
        event = conv.get("event") or ("Purchase" if obj == "sales" else "Lead")
        types = EVENT_ACTIONS.get(event)
        if types is None:
            snake = "".join("_" + ch.lower() if ch.isupper() else ch for ch in event).lstrip("_")
            types = (f"offsite_conversion.fb_pixel_{snake}", snake)
        return EVENT_KIND.get(event, event.lower()), types
    if obj == "leads":
        return "lead", LEAD
    if obj == "sales":
        return "purchase", PURCHASE
    if obj == "awareness":
        return "reach", None
    return "link_click", None


def parse(raw: dict, spec: dict, currency: str) -> dict:
    """One Meta insights row as ad_insights_daily columns."""
    cur = (raw.get("account_currency") or currency or "USD").upper()
    spend = _minor(raw.get("spend"), cur)
    imp, reach = _int(raw.get("impressions")), _int(raw.get("reach"))
    clicks, link = _int(raw.get("clicks")), _int(raw.get("inline_link_clicks"))
    kind, types = goal_of(spec)
    if kind == "reach":
        results = reach
    elif kind == "link_click":
        results = link
    else:
        results = _int(_action(raw.get("actions"), types)[0])
    purchases = _int(_action(raw.get("actions"), PURCHASE)[0])
    value = _minor(_action(raw.get("action_values"), PURCHASE)[0], cur)
    leads = _int(_action(raw.get("actions"), LEAD)[0])
    if kind == "reach":
        cpr = int(round(spend * 1000 / results)) if results else None
    else:
        cpr = int(round(spend / results)) if results else None
    keep = {k: raw.get(k) for k in ("ad_id", "adset_id", "campaign_id", "date_start", "date_stop", "account_currency",
                                    "spend", "impressions", "reach", "clicks", "inline_link_clicks", "ctr", "actions",
                                    "action_values")}
    return {"external_object_id": str(raw.get("ad_id") or ""), "campaign_external_id": str(raw.get("campaign_id") or ""),
            "adset_external_id": str(raw.get("adset_id") or ""), "day": str(raw.get("date_start") or "")[:10],
            "currency": cur, "spend_minor": spend, "impressions": imp, "reach": reach, "clicks": clicks,
            "link_clicks": link, "ctr": round(link * 100.0 / imp, 4) if imp else None,
            "ctr_all": float(_num(raw.get("ctr"))) if raw.get("ctr") not in (None, "") else
            (round(clicks * 100.0 / imp, 4) if imp else None),
            "cpc_minor": int(round(spend / link)) if link else None, "results": results, "result_kind": kind,
            "cost_per_result_minor": cpr, "leads": leads, "purchases": purchases, "purchase_value_minor": value,
            "raw_json": json.dumps(keep, separators=(",", ":"))}


# ---------------------------------------------------------------- the pull

def _launches(conn, conn_row) -> list:
    """The connection's launched campaigns whose campaign is still on Meta."""
    return conn.execute("SELECT l.* FROM launches l JOIN ad_objects o ON o.launch_id=l.id AND o.kind='campaign' AND "
                        "o.status!='DELETED' WHERE l.connection_id=? AND l.workspace_id=? AND l.status='paused' AND "
                        "l.external_campaign_id IS NOT NULL AND l.platform='meta' ORDER BY l.id",
                        (conn_row["id"], conn_row["workspace_id"])).fetchall()


def _sync_statuses(conn, conn_row, token, Ls) -> None:
    """One multi-id read for every launched object of the connection: the page then shows Ads Manager changes too."""
    ids = {"campaign": [], "adset": [], "ad": []}
    by_launch = {}
    for L in Ls:
        obj = admanage.objects(conn, L)
        by_launch[L["id"]] = obj
        for k, v in admanage._ids(obj).items():
            ids[k] += v
    state = adplatforms.adapter("meta").read_objects(token, conn_row["external_account_id"], ids)
    now = db.now()
    for kind, lst in ids.items():
        for oid in lst:
            s = state.get(oid)
            if s is None:
                conn.execute("UPDATE ad_objects SET status='DELETED', effective_status='DELETED', synced_at=? WHERE "
                             "platform='meta' AND external_id=?", (now, oid))
            else:
                conn.execute("UPDATE ad_objects SET status=?, effective_status=?, daily_budget_minor=?, synced_at=? WHERE "
                             "platform='meta' AND external_id=?",
                             (s["status"] or None, s["effective_status"] or None,
                              s["daily_budget"] if kind == "campaign" else None, now, oid))
    conn.commit()


def _fetch(ad, token, account, camp_ids, since: date, until: date) -> list:
    """Meta's rows, splitting the range when Meta says it's too much data."""
    from adplatforms import meta
    try:
        return ad.pull_insights(token, account, camp_ids, since.isoformat(), until.isoformat())
    except meta.TooMuchData:
        if since >= until:
            raise
        mid = since + timedelta(days=(until - since).days // 2)
        return _fetch(ad, token, account, camp_ids, since, mid) + _fetch(ad, token, account, camp_ids,
                                                                          mid + timedelta(days=1), until)


def store(conn, conn_row, Ls, rows: list, since: date, until: date) -> int:
    """Upsert Meta's rows; remove the days Meta no longer reports for these ads inside the pulled window."""
    specs, launch_of_campaign = {}, {}
    for L in Ls:
        try:
            specs[L["id"]] = json.loads(L["spec_json"])
        except ValueError:
            specs[L["id"]] = {}
        launch_of_campaign[str(L["external_campaign_id"])] = L["id"]
    now = db.now()
    seen = set()
    for raw in rows:
        lid = launch_of_campaign.get(str(raw.get("campaign_id") or ""))
        if lid is None or not raw.get("ad_id") or not raw.get("date_start"):
            continue
        r = parse(raw, specs[lid], conn_row["currency"])
        seen.add((r["external_object_id"], r["day"]))
        cols = ["workspace_id", "connection_id", "launch_id", "platform", "level"] + list(r.keys()) + ["fetched_at"]
        vals = [conn_row["workspace_id"], conn_row["id"], lid, "meta", "ad"] + list(r.values()) + [now]
        upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("platform", "external_object_id", "day"))
        conn.execute(f"INSERT INTO ad_insights_daily ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                     f"ON CONFLICT(platform, external_object_id, day) DO UPDATE SET {upd}", vals)
    lids = [L["id"] for L in Ls]
    if lids:
        old = conn.execute(f"SELECT id, external_object_id, day FROM ad_insights_daily WHERE platform='meta' AND launch_id IN "
                           f"({','.join('?' * len(lids))}) AND day BETWEEN ? AND ?",
                           (*lids, since.isoformat(), until.isoformat())).fetchall()
        for o in old:
            if (o["external_object_id"], o["day"]) not in seen:
                conn.execute("DELETE FROM ad_insights_daily WHERE id=?", (o["id"],))
    conn.commit()
    return len(seen)


def _later(conn, conn_row, seconds: float, error: str | None):
    conn.execute("UPDATE ad_connections SET insights_next_at=?, insights_error=? WHERE id=?",
                 (_now_iso(max(60.0, seconds)), error, conn_row["id"]))
    conn.commit()


def pull(conn_id: int) -> str:
    """Pull one connection now. Returns ok | waiting | failed | nothing."""
    with db.connect() as conn:
        conn_row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (conn_id,)).fetchone()
        if conn_row is None or conn_row["status"] != "active" or conn_row["platform"] != "meta":
            return "nothing"
        Ls = _launches(conn, conn_row)
        if not Ls:
            _later(conn, conn_row, every_s(), None)
            return "nothing"
        account = f"act_{conn_row['external_account_id']}"
        held = max(adhttp.blocked_for("meta", account), adhttp.blocked_for("meta", f"{account}:insights"))
        if held > 0:                        # the stored usage headers say wait: don't even ask
            _later(conn, conn_row, held + 5, WORDS["rate"])
            return "waiting"
        try:
            token = adplatforms.open_token(conn_row)
        except secretsbox.BoxError:
            adplatforms.mark(conn, conn_row["id"], "expired", "Meta ended this access. Reconnect.")
            _later(conn, conn_row, RETRY_ERROR_S, WORDS["auth"])
            return "failed"
        until = today(conn_row["timezone"])
        first = min((str(L["approved_at"] or L["created_at"])[:10] for L in Ls), default=until.isoformat())
        try:
            since = max(until - timedelta(days=WINDOW_DAYS - 1), date.fromisoformat(first) - timedelta(days=1))
        except ValueError:
            since = until - timedelta(days=WINDOW_DAYS - 1)
        since = min(since, until)
        ad = adplatforms.adapter("meta")
        try:
            _sync_statuses(conn, conn_row, token, Ls)
            Ls = _launches(conn, conn_row)
            rows = _fetch(ad, token, conn_row["external_account_id"], [L["external_campaign_id"] for L in Ls], since, until)
        except adhttp.RateLimited as e:
            _later(conn, conn_row, max(60.0, float(e.retry_after or 60)), WORDS["rate"])
            log.info("insights for connection %s wait: %s", conn_row["id"], type(e).__name__)
            return "waiting"
        except adhttp.TokenInvalid:
            adplatforms.mark(conn, conn_row["id"], "expired", "Meta ended this access. Reconnect.")
            _later(conn, conn_row, RETRY_ERROR_S, WORDS["auth"])
            return "failed"
        except adhttp.PermissionDenied:
            _later(conn, conn_row, RETRY_ERROR_S, WORDS["permission"])
            return "failed"
        except adhttp.NetworkError:
            _later(conn, conn_row, 15 * 60, WORDS["network"])
            return "failed"
        except adhttp.AdHttpError as e:
            retry = 15 * 60 if (e.status or 0) >= 500 or e.code in adhttp.META_TRANSIENT else RETRY_ERROR_S
            _later(conn, conn_row, retry, WORDS["other"])
            log.warning("insights for connection %s failed: code %s", conn_row["id"], e.code)
            return "failed"
        n = store(conn, conn_row, Ls, rows, since, until)
        conn.execute("UPDATE ad_connections SET insights_at=?, insights_next_at=?, insights_error=NULL WHERE id=?",
                     (db.now(), _now_iso(every_s()), conn_row["id"]))
        conn.commit()
        log.info("insights for connection %s: %s ad-days, %s to %s", conn_row["id"], n, since, until)
        return "ok"


def due(conn, limit: int = PER_TICK) -> list:
    now = _now_iso()
    return [(r["id"], r["workspace_id"]) for r in conn.execute(
        "SELECT c.id, c.workspace_id FROM ad_connections c WHERE c.platform='meta' AND c.status='active' AND "
        "(c.insights_next_at IS NULL OR c.insights_next_at<=?) AND EXISTS (SELECT 1 FROM launches l WHERE "
        "l.connection_id=c.id AND l.status='paused' AND l.platform='meta') ORDER BY COALESCE(c.insights_next_at, '') LIMIT ?",
        (now, limit)).fetchall()]


def tick(limit: int = PER_TICK) -> int:
    """The worker's pass: pull the connections that are due, for workspaces with AD_LAUNCH. Returns pulls tried."""
    n = 0
    with db.connect() as conn:
        items = due(conn, limit)
        wss = {w: conn.execute("SELECT * FROM workspaces WHERE id=?", (w,)).fetchone() for _, w in items}
    for cid, w in items:
        if not adplatforms.enabled(wss.get(w)):
            continue
        try:
            pull(cid)
            n += 1
        except Exception:  # noqa: BLE001  one bad pull never stops the worker
            log.exception("insights pull for connection %s failed", cid)
    return n


def maybe_tick():
    """The worker loop's call: at most every TICK_SECONDS, never raises."""
    if time.time() - _last_tick[0] < TICK_SECONDS:
        return
    _last_tick[0] = time.time()
    try:
        tick()
    except Exception:  # noqa: BLE001
        log.exception("insights tick failed")


def refresh(conn, ws_id: int) -> bool:
    """"Update now": make the workspace's Meta connections due, unless one was pulled in the last REFRESH_MIN_S."""
    rows = conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND platform='meta' AND status='active'",
                        (int(ws_id),)).fetchall()
    cut = (datetime.now(timezone.utc) - timedelta(seconds=REFRESH_MIN_S)).replace(microsecond=0).isoformat()
    moved = False
    for r in rows:
        if r["insights_at"] and r["insights_at"] > cut:
            continue
        conn.execute("UPDATE ad_connections SET insights_next_at=NULL WHERE id=?", (r["id"],))
        moved = True
    conn.commit()
    return moved


# ---------------------------------------------------------------- what the pages read

RANGES = {"today": "Today", "7": "Last 7 days", "14": "Last 14 days"}


def window(tz_name, key: str) -> tuple:
    """(since, until) dates for a range key, in the account's time zone. 7 and 14 are complete days before today,
    like Ads Manager's presets."""
    t = today(tz_name)
    if key == "today":
        return t, t
    n = 14 if key == "14" else 7
    return t - timedelta(days=n), t - timedelta(days=1)


def _sum(conn, where: str, args: tuple) -> dict:
    r = conn.execute(f"SELECT COALESCE(SUM(spend_minor),0) spend, COALESCE(SUM(impressions),0) imp, "
                     f"COALESCE(SUM(reach),0) reach, COALESCE(SUM(clicks),0) clicks, COALESCE(SUM(link_clicks),0) link, "
                     f"COALESCE(SUM(results),0) results, COALESCE(SUM(purchases),0) purchases, "
                     f"COALESCE(SUM(purchase_value_minor),0) value, COALESCE(SUM(leads),0) leads, COUNT(*) n, "
                     f"MAX(result_kind) kind, MAX(currency) cur, COUNT(DISTINCT day) days FROM ad_insights_daily "
                     f"WHERE {where}", args).fetchone()
    return figures(dict(r))


def figures(r: dict) -> dict:
    """Sums plus the rates computed from them (never an average of daily rates)."""
    spend, imp, link, res = r["spend"], r["imp"], r["link"], r["results"]
    kind = r.get("kind") or "link_click"
    out = dict(r)
    out["ctr"] = round(link * 100.0 / imp, 2) if imp else None
    out["cpc"] = int(round(spend / link)) if link else None
    out["cpr"] = (int(round(spend * 1000 / res)) if kind == "reach" else int(round(spend / res))) if res else None
    out["words"] = RESULT_WORDS.get(kind, ("Results", "Cost per result"))
    return out


def launch_figures(conn, L, key: str = "7", tz_name=None) -> dict:
    since, until = window(tz_name, key)
    return _sum(conn, "launch_id=? AND day BETWEEN ? AND ?", (L["id"], since.isoformat(), until.isoformat()))


def ad_figures(conn, L, key: str = "7", tz_name=None) -> dict:
    """{ad external id: figures} for the launch's ads in the range."""
    since, until = window(tz_name, key)
    out = {}
    for r in conn.execute("SELECT external_object_id FROM ad_insights_daily WHERE launch_id=? AND day BETWEEN ? AND ? "
                          "GROUP BY external_object_id", (L["id"], since.isoformat(), until.isoformat())).fetchall():
        out[r["external_object_id"]] = _sum(conn, "launch_id=? AND external_object_id=? AND day BETWEEN ? AND ?",
                                            (L["id"], r["external_object_id"], since.isoformat(), until.isoformat()))
    return out


def daily(conn, L, days: int = 14, tz_name=None) -> list:
    """[(day, spend_minor, results)] for the launch, oldest first, a zero for a day with no delivery."""
    t = today(tz_name)
    rows = {r["day"]: r for r in conn.execute(
        "SELECT day, SUM(spend_minor) spend, SUM(results) results FROM ad_insights_daily WHERE launch_id=? AND day>=? "
        "GROUP BY day", (L["id"], (t - timedelta(days=days - 1)).isoformat())).fetchall()}
    out = []
    for i in range(days - 1, -1, -1):
        d = (t - timedelta(days=i)).isoformat()
        r = rows.get(d)
        out.append((d, int(r["spend"]) if r else 0, int(r["results"]) if r else 0))
    return out


def workspace_totals(conn, ws_id: int, key: str = "7") -> list:
    """[figures with "cur"] per currency, for the workspace's launches in the range (each in its account's days)."""
    out = {}
    for L in conn.execute("SELECT l.*, c.timezone tz FROM launches l JOIN ad_connections c ON c.id=l.connection_id WHERE "
                          "l.workspace_id=? AND l.status='paused' AND l.platform='meta'", (int(ws_id),)).fetchall():
        f = launch_figures(conn, L, key, L["tz"])
        if not f["n"]:
            continue
        cur = f["cur"] or "USD"
        t = out.setdefault(cur, {"spend": 0, "imp": 0, "reach": 0, "clicks": 0, "link": 0, "results": 0, "purchases": 0,
                                 "value": 0, "leads": 0, "n": 0, "cur": cur, "kinds": set()})
        for k in ("spend", "imp", "reach", "clicks", "link", "results", "purchases", "value", "leads", "n"):
            t[k] += f[k] or 0
        t["kinds"].add(f["kind"])
    res = []
    for t in out.values():
        kinds = t.pop("kinds")
        t["kind"] = kinds.pop() if len(kinds) == 1 else None    # results of different kinds aren't added up
        f = figures(t)
        if t["kind"] is None:
            f["results"], f["cpr"] = None, None
        res.append(f)
    return res


def week_over_week(conn, ad_id: str, tz_name=None) -> dict:
    """M4.2's creative fatigue inputs for one ad: the last 7 complete days against the 7 before, each with spend,
    impressions, link clicks and link click rate, plus the changes in %. "complete" is False until the ad has
    delivered on at least one day of each week (a new ad isn't fatigued)."""
    t = today(tz_name)
    weeks = {}
    for name, start, end in (("this", t - timedelta(days=7), t - timedelta(days=1)),
                             ("last", t - timedelta(days=14), t - timedelta(days=8))):
        weeks[name] = _sum(conn, "platform='meta' AND external_object_id=? AND day BETWEEN ? AND ?",
                           (str(ad_id), start.isoformat(), end.isoformat()))
        weeks[name]["since"], weeks[name]["until"] = start.isoformat(), end.isoformat()

    def pct(a, b):
        return round((a - b) * 100.0 / b, 1) if a is not None and b else None
    this, last = weeks["this"], weeks["last"]
    return {"this": this, "last": last, "complete": bool(this["days"] and last["days"]),
            "ctr_change": pct(this["ctr"], last["ctr"]), "spend_change": pct(this["spend"], last["spend"])}


def status(conn, ws_id: int) -> dict:
    """When the numbers were last updated and what went wrong, for the pages."""
    r = conn.execute("SELECT MAX(insights_at) at, MAX(insights_error) err, MIN(insights_next_at) nxt FROM ad_connections "
                     "WHERE workspace_id=? AND platform='meta' AND status IN ('active','expired')", (int(ws_id),)).fetchone()
    return {"at": r["at"], "error": r["err"], "next_at": r["nxt"]}

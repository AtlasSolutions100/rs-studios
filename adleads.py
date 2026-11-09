"""Which ad brought which lead, and what each lead cost (roadmap M4.1, design doc 4.4 "Attribution to the business").

A member's ads send people to Campaignify pages (/p/<slug>) and hosted forms (/f/<code>). The ad's link carries the
tags Campaignify set at launch (launches.UTM for Meta: utm_source=facebook, utm_campaign={{campaign.id}},
utm_content={{ad.id}}; adplatforms.google.URL_SUFFIX for Google: utm_campaign={campaignid}, utm_content={creative})
and the platform's own click id (fbclid, gclid, ttclid). The form on the page keeps them in hidden fields
(hidden_fields), and when the form is sent, record() keeps one lead_sources row per submission:

    * the tags as they came (utm_source, utm_medium, utm_campaign, utm_content, utm_term) and the click id;
    * the ad platform: from utm_source when utm_medium says paid (an organic post tagged utm_source=ig is not an
      ad), else from the click id's kind;
    * the ad and its launch, matched on the workspace's own launched objects (ad_objects): utm_content is the
      ad id, utm_campaign the campaign id. matched = ad | campaign | platform | none.

The contact gets a contact_events row ("ad") so its page says where they came from. Flows run on the new lead
exactly as before (forms_routes.submit enrols form_submitted); nothing here changes them.

The Ad campaigns pages read leads_for() next to what Meta reports: leads on the member's own forms per ad and per
campaign, in the same date range and the ad account's time zone, and cost per lead = the ad's spend from
ad_insights_daily over those leads. A person who sends a form twice in the range is one lead.

Click ids are kept only when the page or form measures visitors (its measurement setting is not 'off'); the
tags are kept either way, as form_submissions always did. Deleting a contact deletes its rows here.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("campaignify.leads")

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
CLICK_PLATFORM = {"fbclid": "meta", "gclid": "google", "ttclid": "tiktok"}
SOURCE_PLATFORM = {"facebook": "meta", "fb": "meta", "meta": "meta", "instagram": "meta", "ig": "meta",
                   "an": "meta", "messenger": "meta", "google": "google", "adwords": "google", "youtube": "google",
                   "tiktok": "tiktok", "snapchat": "snapchat", "snap": "snapchat"}
PAID_MEDIUMS = {"paid", "cpc", "ppc", "cpm", "paid_social", "paidsocial", "paid_search", "ads", "ad", "display",
                "social_paid", "sem"}
PLATFORM_WORDS = {"meta": "Meta", "google": "Google", "tiktok": "TikTok", "snapchat": "Snapchat"}
_UTM_RE = re.compile(r"[^\w.\-+ :/|]")


def _clean_utm(v) -> str:
    return _UTM_RE.sub("", str(v or "").strip())[:120]


def hidden_fields(args, measured: bool = True) -> dict:
    """The hidden fields a form carries from the address its page was opened with: the tags, and (when the page
    measures visitors) the ad click ids as _gclid / _fbclid / _ttclid."""
    out = {}
    if args is None:
        return out
    for k in UTM_KEYS:
        v = _clean_utm(args.get(k))
        if v:
            out[k] = v
    if measured:
        import site_events
        for k, v in site_events.clicks_of(args).items():
            out["_" + k] = v
    return out


def from_form(form, measured: bool = True) -> tuple:
    """(tags, click ids) a sent form carried."""
    utm = {k: _clean_utm(form.get(k)) for k in UTM_KEYS}
    utm = {k: v for k, v in utm.items() if v}
    clicks = {}
    if measured:
        import site_events
        clicks = site_events.clicks_of(form)
    return utm, clicks


def platform_of(utm: dict, clicks: dict) -> str | None:
    """The ad platform of a visit: from utm_source when the medium says paid (an organic post tagged utm_source=ig
    is not an ad), else from the click id's kind."""
    src = (utm.get("utm_source") or "").strip().lower()
    medium = (utm.get("utm_medium") or "").strip().lower().replace("-", "_")
    if src in SOURCE_PLATFORM and (medium in PAID_MEDIUMS or any(clicks.values())):
        return SOURCE_PLATFORM[src]
    for k in ("gclid", "fbclid", "ttclid"):
        if clicks.get(k):
            return CLICK_PLATFORM[k]
    return None


def resolve(conn, ws_id: int, utm: dict, clicks: dict) -> dict:
    """{"platform", "launch_id", "campaign_external_id", "ad_external_id", "matched"} for one lead's tags, matched on
    this workspace's launched objects only."""
    platform = platform_of(utm, clicks)
    out = {"platform": platform, "launch_id": None, "campaign_external_id": None, "ad_external_id": None,
           "matched": "platform" if platform else "none"}
    content = (utm.get("utm_content") or "").strip()
    camp = (utm.get("utm_campaign") or "").strip()
    sql_p, args_p = (" AND platform=?", (platform,)) if platform else ("", ())
    if content:
        row = conn.execute("SELECT launch_id, platform, external_id FROM ad_objects WHERE workspace_id=? AND kind='ad' AND "
                           "external_id=?" + sql_p + " ORDER BY id DESC LIMIT 1", (int(ws_id), content, *args_p)).fetchone()
        if row is not None:
            L = conn.execute("SELECT external_campaign_id FROM launches WHERE id=? AND workspace_id=?",
                             (row["launch_id"], int(ws_id))).fetchone()
            out.update(platform=row["platform"], launch_id=row["launch_id"], ad_external_id=row["external_id"],
                       campaign_external_id=L["external_campaign_id"] if L else None, matched="ad")
            return out
    if camp:
        L = conn.execute("SELECT id, platform, external_campaign_id FROM launches WHERE workspace_id=? AND "
                         "external_campaign_id=?" + sql_p + " ORDER BY id DESC LIMIT 1",
                         (int(ws_id), camp, *args_p)).fetchone()
        if L is not None:
            out.update(platform=L["platform"], launch_id=L["id"], campaign_external_id=L["external_campaign_id"],
                       matched="campaign")
    return out


def record(conn, ws_id: int, *, form, contact_id=None, submission_id=None, page=None, measured: bool = True) -> dict | None:
    """One sent form's source (forms_routes.submit, inside its transaction; no commit). None when the visit carried
    no tags and no click id. Never raises: a lead is saved whatever happens here."""
    try:
        from flask import request
        utm, clicks = from_form(request.form, measured)
        if not utm and not clicks:
            return None
        res = resolve(conn, ws_id, utm, clicks)
        ct = next((k for k in ("gclid", "fbclid", "ttclid") if clicks.get(k)), None)
        now = db.now()
        conn.execute("INSERT OR IGNORE INTO lead_sources (workspace_id, contact_id, submission_id, form_id, page_slug, "
                     "platform, launch_id, campaign_external_id, ad_external_id, matched, utm_source, utm_medium, "
                     "utm_campaign, utm_content, utm_term, click_type, click_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (int(ws_id), contact_id, submission_id, form["id"], page["slug"] if page is not None else None,
                      res["platform"], res["launch_id"], res["campaign_external_id"], res["ad_external_id"],
                      res["matched"], utm.get("utm_source"), utm.get("utm_medium"), utm.get("utm_campaign"),
                      utm.get("utm_content"), utm.get("utm_term"), ct, clicks.get(ct) if ct else None, now))
        if contact_id and res["platform"]:
            conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                         (int(ws_id), contact_id, "ad", str(res["launch_id"] or ""), describe(conn, res)[:200], now))
        return res
    except Exception as e:  # noqa: BLE001
        log.warning("lead source not kept: %s", str(e)[:150])
        return None


def _names(conn, launch_id, ad_id) -> tuple:
    """(campaign name, ad name) of a matched lead, from the launch and its objects."""
    camp, ad = None, None
    if launch_id:
        row = conn.execute("SELECT name FROM ad_objects WHERE launch_id=? AND kind='campaign' ORDER BY id LIMIT 1",
                           (launch_id,)).fetchone()
        camp = row["name"] if row else None
        if not camp:
            L = conn.execute("SELECT spec_json FROM launches WHERE id=?", (launch_id,)).fetchone()
            if L is not None:
                import json
                try:
                    camp = (json.loads(L["spec_json"]) or {}).get("name")
                except ValueError:
                    camp = None
        if ad_id:
            row = conn.execute("SELECT name FROM ad_objects WHERE launch_id=? AND kind='ad' AND external_id=?",
                               (launch_id, ad_id)).fetchone()
            ad = re.sub(r" \[cfy:\d+:[a-z0-9:]+\]$", "", row["name"]) if row and row["name"] else None
            if ad:                 # "<campaign> · ad <n> Evening ritual" -> "Evening ritual" (admanage.ad_name)
                ad = re.sub(r"^.* · ad \d+ (?=\S)", "", ad) or ad
    return camp, ad


def describe(conn, res) -> str:
    """Where a lead came from, in plain words: "Meta ad Evening ritual, campaign Ember Candle"."""
    p = PLATFORM_WORDS.get(res.get("platform") or "", "")
    if not p:
        return "A tagged link"
    camp, ad = _names(conn, res.get("launch_id"), res.get("ad_external_id"))
    if res.get("matched") == "ad":
        return f"{p} ad {ad or res.get('ad_external_id')}" + (f", campaign {camp}" if camp else "")
    if res.get("matched") == "campaign":
        return f"{p} campaign {camp or res.get('campaign_external_id')}"
    return f"A {p} ad"


# ---------------------------------------------------------------- what the pages read

def _bounds(since, until):
    """UTC ISO bounds wide enough to hold every lead of local days since..until in any time zone."""
    lo = datetime(since.year, since.month, since.day, tzinfo=timezone.utc) - timedelta(days=1)
    hi = datetime(until.year, until.month, until.day, tzinfo=timezone.utc) + timedelta(days=2)
    return lo.isoformat(), hi.isoformat()


def _local_day(iso: str, tz_name):
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    try:
        return datetime.fromisoformat(iso).astimezone(tz).date()
    except (TypeError, ValueError):
        return None


def _rows(conn, where: str, args: tuple, since, until, tz_name) -> list:
    lo, hi = _bounds(since, until)
    out = []
    for r in conn.execute("SELECT * FROM lead_sources WHERE " + where + " AND created_at>=? AND created_at<? ORDER BY id",
                          (*args, lo, hi)).fetchall():
        d = _local_day(r["created_at"], tz_name)
        if d is not None and since <= d <= until:
            out.append(r)
    return out


def _who(r) -> str:
    return f"c{r['contact_id']}" if r["contact_id"] else f"s{r['submission_id'] or r['id']}"


def leads_for(conn, L, key: str = "7", tz_name=None, ad_spend: dict | None = None, spend: int | None = None) -> dict:
    """Leads on the member's own forms for one launch in a range: {"total", "by_ad" {ad id: n}, "no_ad" (matched to the
    campaign only), "cpl" (cost per lead over the launch's spend), "ad_cpl" {ad id: cost per lead}}. Spend comes
    from the caller's insights figures, in the account's minor units."""
    import adinsights
    since, until = adinsights.window(tz_name, key)
    return leads_between(conn, L, since, until, tz_name, ad_spend, spend)


def leads_between(conn, L, since, until, tz_name=None, ad_spend: dict | None = None, spend: int | None = None) -> dict:
    """leads_for() over explicit days since..until (the ad account's dates), for the weekly report (M4.4)."""
    rows = _rows(conn, "workspace_id=? AND launch_id=?", (L["workspace_id"], L["id"]), since, until, tz_name)
    people, by_ad, no_ad = set(), {}, set()
    for r in rows:
        who = _who(r)
        people.add(who)
        if r["ad_external_id"]:
            by_ad.setdefault(r["ad_external_id"], set()).add(who)
        else:
            no_ad.add(who)
    out = {"total": len(people), "by_ad": {k: len(v) for k, v in by_ad.items()}, "no_ad": len(no_ad)}
    out["cpl"] = int(round(spend / out["total"])) if spend is not None and out["total"] else None
    out["ad_cpl"] = {}
    for aid, n in out["by_ad"].items():
        s = (ad_spend or {}).get(aid)
        if s is not None and n:
            out["ad_cpl"][aid] = int(round(s / n))
    return out


def unmatched(conn, ws_id: int, key: str = "7", tz_name=None) -> dict:
    """{platform: leads} from ad clicks or ad tags this workspace couldn't match to one of its launches (for example
    ads made in Ads Manager), in the range."""
    import adinsights
    since, until = adinsights.window(tz_name, key)
    out = {}
    for r in _rows(conn, "workspace_id=? AND launch_id IS NULL AND platform IS NOT NULL", (int(ws_id),), since, until,
                   tz_name):
        out.setdefault(r["platform"], set()).add(_who(r))
    return {k: len(v) for k, v in out.items()}


def recent(conn, L, limit: int = 8) -> list:
    """The launch's latest leads with who they are, for the campaign page."""
    out = []
    for r in conn.execute("SELECT s.*, c.name cname, c.email cemail FROM lead_sources s LEFT JOIN contacts c ON "
                          "c.id=s.contact_id AND c.workspace_id=s.workspace_id WHERE s.workspace_id=? AND s.launch_id=? "
                          "ORDER BY s.id DESC LIMIT ?", (L["workspace_id"], L["id"], int(limit))).fetchall():
        out.append(dict(r))
    return out


def of_contact(conn, ws_id: int, contact_id: int) -> list:
    """Where one contact came from, newest first: [{row fields, "words", "launch_ok"}]."""
    out = []
    for r in conn.execute("SELECT * FROM lead_sources WHERE workspace_id=? AND contact_id=? ORDER BY id DESC LIMIT 20",
                          (int(ws_id), int(contact_id))).fetchall():
        d = dict(r)
        d["words"] = describe(conn, d)
        d["platform_word"] = PLATFORM_WORDS.get(d["platform"] or "", "")
        L = conn.execute("SELECT status, platform FROM launches WHERE id=? AND workspace_id=?",
                         (d["launch_id"] or 0, int(ws_id))).fetchone() if d["launch_id"] else None
        d["launch_link"] = bool(L is not None and L["status"] == "paused" and L["platform"] == "meta")
        out.append(d)
    return out


def platforms_by_contact(conn, ws_id: int, contact_ids) -> dict:
    """{contact id: first platform word} for the contacts list's "Meta ad" tags."""
    ids = [int(i) for i in contact_ids]
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        for r in conn.execute(f"SELECT contact_id, platform FROM lead_sources WHERE workspace_id=? AND platform IS NOT NULL "
                              f"AND contact_id IN ({','.join('?' * len(part))}) ORDER BY id", (int(ws_id), *part)):
            out.setdefault(r["contact_id"], PLATFORM_WORDS.get(r["platform"], r["platform"]))
    return out


def forget_contact(conn, ws_id: int, contact_id: int) -> None:
    conn.execute("DELETE FROM lead_sources WHERE workspace_id=? AND contact_id=?", (int(ws_id), int(contact_id)))


def of_submissions(conn, ws_id: int, submission_ids) -> dict:
    """{submission id: lead_sources row} for the form's CSV export."""
    ids = [int(i) for i in submission_ids]
    out = {}
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        if not part:
            continue
        for r in conn.execute(f"SELECT * FROM lead_sources WHERE workspace_id=? AND submission_id IN "
                              f"({','.join('?' * len(part))})", (int(ws_id), *part)):
            out[r["submission_id"]] = r
    return out

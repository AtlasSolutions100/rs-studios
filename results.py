"""The results screen in the new look (/app/results; Koby, Nov 11 2026): what the workspace's campaigns brought in,
from numbers Campaignify holds itself. Nothing here is estimated, projected or compared with anyone else's.

What is counted, and from where:

    Leads                people who sent one of the workspace's forms in the range (form_submissions; a person who
                         sends twice is one lead, as on the Ad campaigns page)
    Link clicks          clicks on its smart links (link_clicks)
    Ads made, approved   the ads finished in the range (jobs), and how many of those a member approved (job_reviews)
    Page views           pages.views: a running count with no dates, so it is shown as "all time" beside a page and
                         never inside the date range
    Spent, cost per lead ad_insights_daily (adinsights.py) and lead_sources (adleads.py): only for campaigns launched
                         from Campaignify on a connected ad account, and only while AD_LAUNCH lets the workspace see
                         ad accounts at all

The range is today and the 6, 29 or 89 days before it. Campaignify's own counts use the workspace's days (its time
zone, else its Meta ad account's, else America/Chicago: weekly_report._tz). An ad account's numbers use that account's
days, as its platform reports them, so a cost per lead is always the spend and the leads of the same days.

Per campaign: a campaign is a desk conversation (desk_threads). Its ads are the jobs its confirmations started (the
made turn's batch); its page and form are the ones the desk made for it (the made turn's links). Spend is not shown
per desk campaign: a launch is made from a planner campaign and does not record which desk campaign its ads came
from. Launched campaigns are listed in their own table, under the name they have on their platform.

"The read" lists facts, each with a link to act on it: the ad with the lowest cost per lead on the member's own
forms, ads wearing out (adfatigue.py's rule), campaigns that spent with nothing to show, and changes waiting for
approval. It never says what a change would bring. Nothing on this page changes anything in an ad account.

New look only (ui_v2.on()); off, the address goes to Studio like the new look's other pages.
"""
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

import db
import ui_v2
from core import is_owner, login_required

bp = Blueprint("results", __name__)

RANGES = (("7", "7 days"), ("30", "30 days"), ("90", "90 days"))
DEFAULT = "7"
CAMPAIGNS_SHOWN = 30        # desk campaigns listed, the one touched last first
SOURCES_SHOWN = 6
LINKS_SHOWN = 5
AD_TOOLS = ("ad_image", "photo_to_video", "motion_copy", "ugc_video")   # what the Library calls Image, Video, Talking
BUSY = ("pending", "running", "made")


def _chunks(ids, n=400):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _marks(ids) -> str:
    return ",".join("?" * len(ids))


def _who(r) -> str:
    return f"c{r['contact_id']}" if r["contact_id"] else f"s{r['id']}"


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def window(tz, days: int) -> tuple:
    """(since, until, lo): today and the days - 1 before it as dates in `tz`, and the UTC time `since` starts at,
    written as the database writes times (db.now())."""
    import adinsights
    until = adinsights.today(getattr(tz, "key", None))
    since = until - timedelta(days=days - 1)
    lo = datetime(since.year, since.month, since.day, tzinfo=tz).astimezone(timezone.utc).replace(microsecond=0)
    return since, until, lo.isoformat()


# ---------------------------------------------------------------- Campaignify's own counts

def own(conn, ws_id: int, lo: str) -> dict:
    """Leads, link clicks and ads made since `lo`, with where the leads came from and the links clicked most."""
    import adleads
    subs = conn.execute("SELECT id, contact_id, link_code, utm_source FROM form_submissions WHERE workspace_id=? AND "
                        "created_at>=?", (ws_id, lo)).fetchall()
    paid = {}
    for part in _chunks([s["id"] for s in subs]):
        for r in conn.execute(f"SELECT submission_id, platform FROM lead_sources WHERE workspace_id=? AND platform IS "
                              f"NOT NULL AND submission_id IN ({_marks(part)})", (ws_id, *part)).fetchall():
            paid[r["submission_id"]] = r["platform"]
    by = {}
    for s in subs:
        if s["id"] in paid:
            label = f"{adleads.PLATFORM_WORDS.get(paid[s['id']], paid[s['id']].title())} ads"
        elif (s["utm_source"] or "").strip():
            label = s["utm_source"].strip()[:40]
        elif s["link_code"]:
            label = "Smart links"
        else:
            label = "Not tagged"
        by.setdefault(label, set()).add(_who(s))
    sources = sorted(((k, len(v)) for k, v in by.items()), key=lambda x: (x[0] == "Not tagged", -x[1], x[0]))
    clicks = conn.execute("SELECT COUNT(*) FROM link_clicks c JOIN links l ON l.id=c.link_id WHERE l.workspace_id=? AND "
                          "c.at>=?", (ws_id, lo)).fetchone()[0]
    links = [{"name": r["title"] or f"/l/{r['code']}", "n": r["n"]} for r in conn.execute(
        "SELECT l.id, l.code, l.title, COUNT(*) n FROM link_clicks c JOIN links l ON l.id=c.link_id WHERE l.workspace_id=? "
        "AND c.at>=? GROUP BY l.id ORDER BY n DESC, l.id LIMIT ?", (ws_id, lo, LINKS_SHOWN)).fetchall()]
    made = [r["id"] for r in conn.execute(
        f"SELECT id FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND status='done' AND tool IN ({_marks(AD_TOOLS)}) "
        f"AND created_at>=?", (ws_id, *AD_TOOLS, lo)).fetchall()]
    return {"leads": len({_who(s) for s in subs}), "sent": len(subs), "sources": sources[:SOURCES_SHOWN],
            "clicks": clicks, "links": links, "made": len(made), "approved": len(_approved(conn, ws_id, made))}


def _approved(conn, ws_id: int, job_ids) -> set:
    """The jobs a member approved and nobody turned down."""
    ok, no = set(), set()
    for part in _chunks(job_ids):
        for r in conn.execute(f"SELECT job_id, verdict FROM job_reviews WHERE workspace_id=? AND job_id IN "
                              f"({_marks(part)})", (ws_id, *part)).fetchall():
            (ok if r["verdict"] == "approved" else no).add(r["job_id"])
    return ok - no


# ---------------------------------------------------------------- per campaign (a desk conversation)

def _made_of(conn, ws_id: int, tid: int) -> tuple:
    """What one desk campaign made: (batches of ads, page ids, form ids). The page and the form are read from the
    links the made turns kept, matched against the app's own addresses."""
    adapter = current_app.url_map.bind("")
    batches, page_ids, form_ids = set(), set(), set()
    for r in conn.execute("SELECT body_json FROM desk_turns WHERE thread_id=? AND workspace_id=? AND kind='made'",
                          (tid, ws_id)).fetchall():
        try:
            body = json.loads(r["body_json"] or "{}")
        except ValueError:
            continue
        if not isinstance(body, dict):
            continue
        if str(body.get("batch") or "").isdigit():
            batches.add(int(body["batch"]))
        for m in body.get("made") or []:
            if not isinstance(m, dict) or m.get("kind") not in ("page", "form"):
                continue
            try:
                ep, args = adapter.match(str(m.get("href") or "").split("#")[0].split("?")[0], method="GET")
            except Exception:  # noqa: BLE001 — an address the app no longer has
                continue
            if ep == "pages.edit" and args.get("pid"):
                page_ids.add(int(args["pid"]))
            elif ep == "forms.edit" and args.get("fid"):
                form_ids.add(int(args["fid"]))
    return batches, page_ids, form_ids


def campaigns(conn, ws_id: int, lo: str) -> list:
    """The workspace's desk campaigns, the one touched last first: its ads (made, being made, approved), its page
    (with its views, all time) and the leads its form or page brought since `lo`."""
    out = []
    for t in conn.execute("SELECT * FROM desk_threads WHERE workspace_id=? AND archived=0 ORDER BY updated_at DESC, id DESC "
                          "LIMIT ?", (ws_id, CAMPAIGNS_SHOWN)).fetchall():
        batches, page_ids, form_ids = _made_of(conn, ws_id, t["id"])
        jobs = conn.execute(f"SELECT id, status FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND batch_id IN "
                            f"({_marks(batches)})", (ws_id, *batches)).fetchall() if batches else []
        done = [j["id"] for j in jobs if j["status"] == "done"]
        pages_ = conn.execute(f"SELECT id, slug, title, status, views FROM pages WHERE workspace_id=? AND id IN "
                              f"({_marks(page_ids)}) ORDER BY id", (ws_id, *page_ids)).fetchall() if page_ids else []
        forms_ = [r["id"] for r in conn.execute(
            f"SELECT id FROM forms WHERE workspace_id=? AND id IN ({_marks(form_ids)})", (ws_id, *form_ids)).fetchall()] \
            if form_ids else []
        leads = None
        if pages_ or forms_:
            where, args = [], []
            if forms_:
                where.append(f"form_id IN ({_marks(forms_)})")
                args += forms_
            if pages_:
                where.append(f"page_slug IN ({_marks(pages_)})")
                args += [p["slug"] for p in pages_]
            leads = len({_who(s) for s in conn.execute(
                f"SELECT id, contact_id FROM form_submissions WHERE workspace_id=? AND created_at>=? AND "
                f"({' OR '.join(where)})", (ws_id, lo, *args)).fetchall()})
        out.append({"id": t["id"], "title": t["title"] or "A campaign", "href": url_for("desk.thread", tid=t["id"]),
                    "ads": len(done), "busy": sum(1 for j in jobs if j["status"] in BUSY),
                    "approved": len(_approved(conn, ws_id, done)), "leads": leads,
                    "pages": [{"title": p["title"], "live": p["status"] == "published", "views": p["views"],
                               "href": url_for("pages.edit", pid=p["id"])} for p in pages_]})
    return out


# ---------------------------------------------------------------- the ad accounts

def ads(conn, ws, user, days: int) -> dict | None:
    """What the connected ad accounts report for the campaigns launched from Campaignify, with the leads those ads
    brought on the member's own forms. None when the workspace can't see ad accounts (AD_LAUNCH)."""
    import adfatigue
    import adinsights
    import adleads
    import admanage
    import adplatforms
    import app as appmod
    import launches
    import weekly_report
    if not adplatforms.enabled(ws, user):
        return None
    has = appmod._has_endpoint
    seen = {}
    for c in conn.execute("SELECT platform, status FROM ad_connections WHERE workspace_id=? AND status IN "
                          "('active','expired','error') ORDER BY id", (ws["id"],)).fetchall():
        seen[c["platform"]] = seen.get(c["platform"], False) or c["status"] == "active"
    out = {"conns": [{"word": adinsights.PLATFORM_WORDS.get(p, p.title()), "ok": ok} for p, ok in seen.items()],
           "groups": [], "quiet": 0, "launched": 0, "read": [], "totals": [], "other": {},
           "status": adinsights.status(conn, ws["id"]),
           "connect": url_for("adconnect.settings") if has("adconnect.settings") else None,
           "all": url_for("manage.index") if has("manage.index") else None}
    totals, by_cur, waiting, tired_items, empty = {}, {}, [], [], []
    for s in admanage.listed(conn, ws["id"]):
        if not s["reported"]:
            continue
        out["launched"] += 1
        L, cr = s["launch"], s["conn_row"]
        tz = cr["timezone"] if cr is not None else None
        until = adinsights.today(tz)
        since = until - timedelta(days=days - 1)
        a_, b_ = since.isoformat(), until.isoformat()
        f = adinsights._sum(conn, "launch_id=? AND day BETWEEN ? AND ?", (L["id"], a_, b_))
        ad_fig = {r["external_object_id"]: adinsights._sum(
            conn, "launch_id=? AND external_object_id=? AND day BETWEEN ? AND ?", (L["id"], r["external_object_id"], a_, b_))
            for r in conn.execute("SELECT DISTINCT external_object_id FROM ad_insights_daily WHERE launch_id=? AND day "
                                  "BETWEEN ? AND ?", (L["id"], a_, b_)).fetchall()}
        lf = adleads.leads_between(conn, L, since, until, tz, {k: v["spend"] for k, v in ad_fig.items()},
                                   f["spend"] if f["n"] else None)
        cur = f["cur"] or s["currency"] or "USD"
        money = lambda minor, cur=cur: launches.money(int(minor), cur)        # noqa: E731
        href = url_for("manage.show", lid=L["id"]) if s["manageable"] and has("manage.show") else s["manager"]
        inside = bool(s["manageable"] and has("manage.show"))
        tired = {a["id"]: a for a in adfatigue.for_launch(conn, L, s=s)["tired"]} if s["manageable"] else {}
        if s["pending"]:
            waiting.append({"kind": "waiting", "label": "Waiting", "text": f"A change to {s['name']} is waiting for "
                            f"approval: {s['pending']['card'].get('title') or 'a change'}.",
                            "href": (href + "#card") if inside else href,
                            "cta": "Check the card" if is_owner() else "See the change"})
        if not (f["n"] or lf["total"] or s["on"] or s["pending"] or tired):
            out["quiet"] += 1
            continue
        t = totals.setdefault(cur, {"cur": cur, "spend": 0, "leads": 0})
        t["spend"] += f["spend"]
        t["leads"] += lf["total"]
        rows = []
        if s["platform"] == "meta":
            for a in s["objects"]["ads"]:
                row, n = a["row"], a["n"]
                aid = row["external_id"]
                af = ad_fig.get(aid)
                name = admanage.ad_name(row, n, s["name"]) or f"Ad {n}"
                rows.append({"name": name, "spend": money(af["spend"]) if af else None,
                             "results": weekly_report._n(af["results"], af["words"][0]) if af and af["results"] else None,
                             "leads": lf["by_ad"].get(aid, 0), "cpl": money(lf["ad_cpl"][aid]) if aid in lf["ad_cpl"]
                             else None, "state": "Wearing out" if aid in tired else admanage.status_word(row)[0],
                             "worn": aid in tired})
                if lf["by_ad"].get(aid) and aid in lf["ad_cpl"]:
                    by_cur.setdefault(cur, []).append({"name": name, "camp": s["name"], "leads": lf["by_ad"][aid],
                                                       "cpl": lf["ad_cpl"][aid], "href": href, "inside": inside})
                if aid in tired:
                    tired_items.append({"kind": "tired", "label": "Wearing out", "text": f"{name} in {s['name']}. "
                                        f"{adfatigue.words(tired[aid])}",
                                        "href": url_for("create.index", refresh=f"{L['id']}:{aid}") if has("create.index")
                                        else href, "cta": "Make a fresh version"})
        out["groups"].append({"name": s["name"], "where": s["platform_word"], "state": s["word"], "href": href,
                              "inside": inside, "manager": s["manager_word"], "rows": rows,
                              "spend": money(f["spend"]) if f["n"] else None,
                              "results": weekly_report._n(f["results"], f["words"][0]) if f["n"] and f["results"] else None,
                              "leads": lf["total"], "cpl": money(lf["cpl"]) if lf["cpl"] is not None else None})
        if f["n"] and f["spend"] and not f["results"] and not lf["total"]:
            empty.append({"kind": "none", "label": "No leads", "text": f"{s['name']} spent {money(f['spend'])} in these "
                          f"{days} days and brought no {f['words'][0].lower()} and no leads on your forms.",
                          "href": href, "cta": "Open the campaign" if inside else f"Open {s['manager_word']}"})
    best = []
    for cur, cands in by_cur.items():
        top = min(cands, key=lambda c: (c["cpl"], -c["leads"]))
        each = launches.money(int(top["cpl"]), cur)
        if len(cands) == 1:
            text = (f"{top['name']} in {top['camp']} is the only ad that brought leads on your forms: "
                    f"{_n(top['leads'], 'lead')} at {each} each.")
        else:
            text = (f"{top['name']} in {top['camp']} brought {top['leads']:,} of the {totals[cur]['leads']:,} leads your "
                    f"ads sent to your forms, at {each} each. That is the lowest cost per lead of your ads.")
        best.append({"kind": "best", "label": "Best", "text": text, "href": top["href"],
                     "cta": "Open the campaign" if top["inside"] else "Open the ads manager"})
    out["read"] = best + tired_items + empty + waiting
    out["totals"] = [{"cur": t["cur"], "spend": launches.money(int(t["spend"]), t["cur"]), "leads": t["leads"],
                      "cpl": launches.money(int(round(t["spend"] / t["leads"])), t["cur"]) if t["leads"] and t["spend"]
                      else None} for t in totals.values() if t["spend"] or t["leads"]]
    # leads from ad clicks or ad tags that match none of this workspace's launches (ads started in an ads manager)
    tz = getattr(_tz(conn, ws), "key", None)
    until = adinsights.today(tz)
    other = {}
    for r in adleads._rows(conn, "workspace_id=? AND launch_id IS NULL AND platform IS NOT NULL", (int(ws["id"]),),
                           until - timedelta(days=days - 1), until, tz):
        other.setdefault(r["platform"], set()).add(adleads._who(r))
    out["other"] = {adleads.PLATFORM_WORDS.get(k, k.title()): len(v) for k, v in other.items()}
    return out


def _tz(conn, ws):
    import weekly_report
    return weekly_report._tz(conn, ws)


# ---------------------------------------------------------------- the page

@bp.route("/app/results")
@login_required
def page():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    key = request.args.get("range") if request.args.get("range") in dict(RANGES) else DEFAULT
    days = int(key)
    import app as appmod
    has = appmod._has_endpoint
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        since, until, lo = window(_tz(conn, ws), days)
        mine = own(conn, ws["id"], lo)
        camps = campaigns(conn, ws["id"], lo)
        ad = ads(conn, ws, g.user, days)
        has_form = conn.execute("SELECT 1 FROM forms WHERE workspace_id=? AND archived=0 LIMIT 1", (ws["id"],)).fetchone()
        has_link = conn.execute("SELECT 1 FROM links WHERE workspace_id=? AND archived=0 LIMIT 1", (ws["id"],)).fetchone()
    to = {"leads": url_for("forms.leads") if has("forms.leads") else None,
          "forms": url_for("forms.index") if has("forms.index") else None,
          "links": url_for("pages.links") if has("pages.links") else None,
          "library": url_for("library"), "desk": url_for("desk.page"),
          "setup": url_for("adhub.index") if has("adhub.index") else None}
    return render_template("v2/results.html", key=key, days=days, ranges=RANGES, since=since, until=until, mine=mine,
                           camps=camps, ad=ad, to=to, has_form=bool(has_form), has_link=bool(has_link))

"""Manage a launched campaign from Campaignify (roadmap M2.4, design doc 4.3): turn a campaign or an ad on or off,
and change the campaign's daily budget. Every change goes through an approval card.

    1. A member asks for a change on the Campaigns page (manage_routes.py). draft() reads the objects from Meta
       first (so "before" is what Meta has now, not what we last stored), works out exactly which objects change
       and how, checks the change (rules()), and stores an undecided card (approval_cards kind status_change or
       budget_change). An older undecided card of the same campaign is replaced.
    2. The owner (or an admin) approves it (approve()). That reads Meta again: when anything on the card changed
       on Meta in between (someone used Ads Manager), the card is refused and the member drafts a new one. The
       rules run again on the workspace as it is now (D2 paid plan, billing hold, D3 cap in the ad account's
       currency). A raise of more than half the budget of 24 hours ago needs a second confirmation; turning on an
       ad made with AI needs the member to confirm Meta's AI label is ticked in Ads Manager (Meta has no API
       field for it yet, adplatforms/meta.AI_DISCLOSURE_FIELD).
    3. apply() makes the changes, one POST per object, in a safe order (turning on: the ads, then the ad set, then
       the campaign, so nothing starts before everything is ready; pausing: the campaign only). Each change takes
       the object's 30 s edit slot and spends from the ad account's rate budget (adhttp). When Meta or our budget
       says wait, the card waits (apply_state waiting, next_at) and the worker finishes it (tick()). A change is
       idempotent on Meta (setting a status or a budget it already has does nothing), so a retry after a crash
       is safe. Progress is kept per change in the card (after_json.changes[].done).

What changes is only ever what the card lists. Members draft, owners approve (core.is_owner: owner or admin);
an admin viewing another workspace is read-only. Who drafted, who approved, when, and what Meta answered stay
on the card. Pausing is always allowed; turning on and raising the budget need a paid plan and stay under the
plan's daily cap (launches.ceiling, D3). Everything sits behind AD_LAUNCH (adplatforms.enabled). Meta launches only: a Google launch (launch_google.py,
M3.2) is managed in Google Ads until its own manage task.
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import adhttp
import adplatforms
import adpolicy
import adspec
import db
import launches
import plans
import secretsbox
from adspec import Issue

log = logging.getLogger("campaignify.manage")

KINDS = ("status_change", "budget_change")
BIG_RAISE = 1.5               # design doc 4.3: more than +50% on the budget of 24 hours ago needs a second confirm
RAISE_WINDOW_S = 24 * 3600
LEASE_S = 120                 # an apply that died holds its card this long
WAIT_MAX_S = 2 * 3600         # a change that keeps waiting on Meta longer than this fails
NETWORK_TRIES = 3             # network or Meta 5xx: tries before the change fails
RETRY_GAP_S = 15.0
SYNC_EVERY_S = 120            # the Campaigns page reads Meta again at most this often per campaign
TICK_SECONDS = 10
CLOCK = time.time             # tests move it
_last_tick = [0.0]

STATUS_WORDS = {"ACTIVE": "On", "PAUSED": "Paused"}
KIND_WORDS = {"campaign": "Campaign", "adset": "Ad set", "ad": "Ad"}
# Meta's effective_status in plain words, and the tag colour (ok / warn / bad / "").
EFFECTIVE = {"ACTIVE": ("On", "ok"), "PAUSED": ("Paused", ""), "CAMPAIGN_PAUSED": ("Waits for the campaign", ""),
             "ADSET_PAUSED": ("Waits for the ad set", ""), "IN_PROCESS": ("Meta is updating it", "warn"),
             "PENDING_REVIEW": ("In review at Meta", "warn"), "PREAPPROVED": ("Approved, starting soon", "warn"),
             "DISAPPROVED": ("Not approved by Meta", "bad"), "WITH_ISSUES": ("Has a problem on Meta", "bad"),
             "PENDING_BILLING_INFO": ("Needs a payment method on Meta", "bad"), "ARCHIVED": ("Archived on Meta", ""),
             "DELETED": ("Deleted on Meta", "")}
STATE_WORDS = {"queued": "Being made", "applying": "Being made", "waiting": "Waiting for Meta", "done": "Done",
               "failed": "Failed"}


class NotApproved(Exception):
    """The card can't be approved; str() is the reason in plain words."""


class Refused(Exception):
    """A change can't be drafted (Meta unreachable, nothing to change); str() is the reason in plain words."""


# ---------------------------------------------------------------- small helpers

def _now_iso(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(CLOCK() + offset_s, timezone.utc).replace(microsecond=0).isoformat()


def _age_s(iso) -> float:
    try:
        return CLOCK() - datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def on(ws, user=None) -> bool:
    return adplatforms.enabled(ws, user)


def money(minor, currency) -> str:
    return launches.money(int(minor), currency) if minor is not None else "no daily budget"


def launch_of(conn, ws_id: int, lid: int):
    """A Meta launch of this workspace, or None (Google launches are managed in Google Ads for now)."""
    return conn.execute("SELECT * FROM launches WHERE id=? AND workspace_id=? AND platform='meta'",
                        (int(lid), int(ws_id))).fetchone()


def connection_of(conn, L):
    return conn.execute("SELECT * FROM ad_connections WHERE id=? AND workspace_id=?",
                        (L["connection_id"], L["workspace_id"])).fetchone()


def objects(conn, L) -> dict:
    """The launch's live objects: {"campaign": row, "adset": row, "ads": [{"n", "row"}]} (ads in launch order)."""
    rows = {r["external_id"]: r for r in conn.execute(
        "SELECT * FROM ad_objects WHERE launch_id=? AND status!='DELETED'", (L["id"],)).fetchall()}
    steps = conn.execute("SELECT step_key, kind, external_id FROM launch_steps WHERE launch_id=? AND status='done' "
                         "AND kind IN ('campaign','adset','ad') ORDER BY step_no", (L["id"],)).fetchall()
    out = {"campaign": None, "adset": None, "ads": []}
    for s in steps:
        r = rows.get(s["external_id"])
        if r is None:
            continue
        if s["kind"] == "ad":
            out["ads"].append({"n": int(s["step_key"].split(":")[1]), "row": r})
        else:
            out[s["kind"]] = r
    return out


def budget_of(L, camp) -> int | None:
    """The campaign's daily budget as we last knew it (Meta's last answer, else what the launch made)."""
    if camp is not None and camp["daily_budget_minor"] is not None:
        return int(camp["daily_budget_minor"])
    try:
        return int(json.loads(L["spec_json"])["daily_budget_minor"])
    except (ValueError, KeyError, TypeError):
        return None


def ai_ads(conn, L) -> dict:
    """{ad number: "presenter" | "media"} for the launch's ads that use AI (the same reading as the launch card:
    the spec's flags, forced from the library at approval, plus the library's AI-made media)."""
    try:
        spec = json.loads(L["spec_json"])
    except ValueError:
        return {}
    creatives = spec.get("creatives") or []
    prov = adpolicy.provenance(conn, L["workspace_id"], [c.get("asset_id") for c in creatives if c.get("asset_id")])
    out = {}
    for i, c in enumerate(creatives, start=1):
        if c.get("ai_presenter") or c.get("synthetic_voice"):
            out[i] = "presenter"
        elif (prov.get(c.get("asset_id")) or {}).get("ai_generated"):
            out[i] = "media"
    return out


def ai_line(kinds: dict) -> str:
    """"Ad 2 has an AI presenter or AI voice; ad 1 is AI-made media." for {2: "presenter", 1: "media"}."""
    pres = sorted(n for n, k in kinds.items() if k == "presenter")
    made = sorted(n for n, k in kinds.items() if k == "media")
    parts = []
    if pres:
        parts.append(f"{launches._ad_list(pres)} {'has' if len(pres) == 1 else 'have'} an AI presenter or AI voice")
    if made:
        parts.append(f"{launches._ad_list(made)} {'is' if len(made) == 1 else 'are'} AI-made media")
    s = "; ".join(parts) + "."
    return s[:1].upper() + s[1:]


def status_word(row) -> tuple:
    """(words, tag class) for an ad_objects row: what it is set to on Meta and what Meta does with it."""
    if row is None:
        return ("Not on Meta", "")
    st, eff = (row["status"] or "").upper(), (row["effective_status"] or "").upper()
    if st in ("DELETED", "ARCHIVED") or eff in ("DELETED", "ARCHIVED"):
        return EFFECTIVE[eff if eff in EFFECTIVE else st]
    if eff in ("DISAPPROVED", "WITH_ISSUES", "PENDING_BILLING_INFO"):      # a problem shows even while paused
        return EFFECTIVE[eff]
    if st == "PAUSED":
        return ("Paused", "")
    if st == "ACTIVE":
        if eff in ("", "ACTIVE"):
            return ("On", "ok")
        return EFFECTIVE.get(eff, ("On", "ok"))
    return (st.title() or "Unknown", "")


# ---------------------------------------------------------------- reading Meta

def _ids(obj: dict) -> dict:
    return {"campaign": [obj["campaign"]["external_id"]] if obj["campaign"] is not None else [],
            "adset": [obj["adset"]["external_id"]] if obj["adset"] is not None else [],
            "ad": [a["row"]["external_id"] for a in obj["ads"]]}


def _token(conn, conn_row) -> str:
    if conn_row is None or conn_row["status"] != "active":
        raise Refused("The Meta ad account needs Reconnect. Go to Settings > Connections.")
    try:
        return adplatforms.open_token(conn_row)
    except secretsbox.BoxError:
        raise Refused("The Meta ad account needs Reconnect. Go to Settings > Connections.") from None


def read(conn, L, conn_row, token, obj=None) -> dict:
    """Meta's current state of the launch's objects ({id: state}, adplatforms/meta.read_objects), stored on
    ad_objects (status, effective_status, daily budget, synced_at). An object Meta no longer returns is marked
    DELETED. Raises adhttp errors."""
    obj = obj or objects(conn, L)
    ids = _ids(obj)
    state = adplatforms.adapter(L["platform"]).read_objects(token, conn_row["external_account_id"], ids)
    now = db.now()
    for kind, lst in ids.items():
        for oid in lst:
            s = state.get(oid)
            if s is None:
                conn.execute("UPDATE ad_objects SET status='DELETED', effective_status='DELETED', synced_at=? "
                             "WHERE platform=? AND external_id=?", (now, L["platform"], oid))
                continue
            conn.execute("UPDATE ad_objects SET status=?, effective_status=?, daily_budget_minor=?, synced_at=? "
                         "WHERE platform=? AND external_id=?",
                         (s["status"] or None, s["effective_status"] or None,
                          s["daily_budget"] if kind == "campaign" else None, now, L["platform"], oid))
    conn.commit()
    return state


def sync(conn, L, force: bool = False) -> str | None:
    """Read the campaign from Meta when the stored state is older than SYNC_EVERY_S (the Campaigns page calls it).
    Returns None, or what went wrong in plain words (the page then shows what we last knew)."""
    if L["status"] != "paused":
        return None
    obj = objects(conn, L)
    if obj["campaign"] is None:
        return None
    if not force and obj["campaign"]["synced_at"] and _age_real(obj["campaign"]["synced_at"]) < SYNC_EVERY_S:
        return None
    conn_row = connection_of(conn, L)
    try:
        read(conn, L, conn_row, _token(conn, conn_row), obj)
    except Refused as e:
        return str(e)
    except adhttp.TokenInvalid:
        adplatforms.mark(conn, conn_row["id"], "expired", "Meta ended this access. Reconnect.")
        return launches.ERRORS[(190, None)]
    except adhttp.AdHttpError as e:
        log.info("sync of launch %s: %s", L["id"], type(e).__name__)
        return "We couldn't read the campaign from Meta just now, so this shows what we last saw."
    return None


def _age_real(iso) -> float:
    try:
        return time.time() - datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 1e9


# ---------------------------------------------------------------- what a change does

def _applied_targets(conn, L) -> set:
    """Ads (external ids) switched on or off from Campaignify since the launch: turning the campaign on leaves them
    as the member set them."""
    out = set()
    for r in conn.execute("SELECT after_json FROM approval_cards WHERE launch_id=? AND kind='status_change' AND "
                          "decision='approved' AND apply_state='done'", (L["id"],)).fetchall():
        a = json.loads(r["after_json"] or "{}")
        if a.get("target") not in (None, "campaign"):
            out.add(a["target"])
    return out


def ad_name(row, n, campaign_name: str = "") -> str:
    """An ad's own name without the "<campaign> · ad <n> " the launch put in front ("Evening ritual")."""
    name = ((row["name"] if row is not None else "") or "").strip()
    for prefix in (f"{campaign_name} · ad {n} ", f"{campaign_name} · ad {n}"):
        if campaign_name and name.startswith(prefix) and name[len(prefix):].strip():
            return name[len(prefix):].strip()
    return name


def _label(kind, row, n=None, campaign_name: str = "") -> str:
    if kind == "ad":
        short = ad_name(row, n, campaign_name)
        return f"Ad {n}, {short}" if short else f"Ad {n}"
    return {"campaign": "The campaign", "adset": "The ad set"}[kind]


def plan_changes(conn, L, obj, state, action, target, budget_after=None) -> list:
    """The exact changes, in the order they are made: [{"id", "kind", "n", "label", "field", "before", "after"}].
    `state` is Meta's current state (read())."""
    def st(row):
        return (state.get(row["external_id"]) or {}).get("status") or row["status"]
    camp, adset = obj["campaign"], obj["adset"]
    out = []
    if action == "budget":
        before = (state.get(camp["external_id"]) or {}).get("daily_budget")
        if budget_after is not None and before != int(budget_after):
            out.append({"id": camp["external_id"], "kind": "campaign", "n": None, "label": _label("campaign", camp),
                        "field": "daily_budget", "before": before, "after": int(budget_after)})
        return out
    want = "ACTIVE" if action == "on" else "PAUSED"
    if target == "campaign":
        if action == "on":
            held = _applied_targets(conn, L)
            for a in obj["ads"]:
                r = a["row"]
                if r["external_id"] not in held and st(r) == "PAUSED":
                    out.append({"id": r["external_id"], "kind": "ad", "n": a["n"],
                                "label": _label("ad", r, a["n"], camp["name"]), "field": "status", "before": "PAUSED",
                                "after": "ACTIVE"})
            if adset is not None and st(adset) == "PAUSED":
                out.append({"id": adset["external_id"], "kind": "adset", "n": None, "label": _label("adset", adset),
                            "field": "status", "before": "PAUSED", "after": "ACTIVE"})
        if st(camp) != want:
            out.append({"id": camp["external_id"], "kind": "campaign", "n": None, "label": _label("campaign", camp),
                        "field": "status", "before": st(camp), "after": want})
        return out
    a = next((x for x in obj["ads"] if x["row"]["external_id"] == str(target)), None)
    if a is not None and st(a["row"]) != want:
        out.append({"id": a["row"]["external_id"], "kind": "ad", "n": a["n"],
                    "label": _label("ad", a["row"], a["n"], camp["name"]), "field": "status", "before": st(a["row"]),
                    "after": want})
    return out


def _raise_base(conn, L, current) -> int | None:
    """The budget 24 hours ago: the "before" of the first budget change applied in the last 24 hours, else now's."""
    cut = (datetime.now(timezone.utc) - timedelta(seconds=RAISE_WINDOW_S)).replace(microsecond=0).isoformat()
    r = conn.execute("SELECT before_json FROM approval_cards WHERE launch_id=? AND kind='budget_change' AND "
                     "decision='approved' AND apply_state='done' AND applied_at>=? ORDER BY applied_at, id LIMIT 1",
                     (L["id"], cut)).fetchone()
    if r:
        try:
            return int(json.loads(r["before_json"])["budget"])
        except (ValueError, KeyError, TypeError):
            pass
    return current


def rules(conn, ws, L, conn_row, action, target, changes, state, budget_after=None, operator=False) -> list:
    """What blocks or warns about a change, on the workspace as it is now."""
    out = []
    add = lambda *a: out.append(Issue(*a))  # noqa: E731
    obj = objects(conn, L)
    camp = obj["campaign"]
    cur = (conn_row["currency"] if conn_row is not None else None) or json.loads(L["spec_json"]).get("currency") or "USD"
    if conn_row is None or conn_row["status"] != "active" or not conn_row["token_enc"]:
        add("block", "manage.no_connection", "The Meta ad account needs Reconnect.", "Reconnect it in Settings > "
            "Connections.", "connection")
    if L["status"] != "paused" or camp is None:
        add("block", "manage.not_launched", "This campaign isn't on Meta yet.", "Wait for the launch to finish.", "launch")
        return out
    budget_now = (state.get(camp["external_id"]) or {}).get("daily_budget")
    if not changes:
        words = {"on": "It's already on.", "off": "It's already paused."}.get(action, "That's the budget it has now.")
        add("block", "manage.no_change", words, "Nothing to change.", "action")
    spends_more = action == "on" or (action == "budget" and budget_now is not None and budget_after is not None
                                     and budget_after > budget_now)
    if action == "budget":
        if budget_now is None:
            add("block", "manage.budget_unknown", "This campaign has no daily campaign budget on Meta now, so the "
                "budget can't be changed here.", "Change it in Ads Manager.", "daily_budget")
        if budget_after is None or budget_after < adspec.MIN_DAILY["meta"]:
            add("block", "manage.budget_min", "That daily budget is under Meta's minimum.",
                f"Set at least {money(adspec.MIN_DAILY['meta'], cur)} a day.", "daily_budget")
    if spends_more:
        code = launches.launch_plan_code(ws, operator)
        if code == "free":
            add("block", "manage.paid_plan", "Turning campaigns on and raising budgets from Campaignify needs an "
                "active subscription.", "Pick a plan on the Billing page, or make the change in Ads Manager.", "plan")
        import billing_holds
        if billing_holds.reason(ws):
            add("block", "manage.billing_hold", "This workspace is on hold, so nothing can be turned on or raised.",
                "See the banner at the top of the page.", "plan")
        in_force = budget_after if action == "budget" else budget_now
        cap = launches.ceiling(ws, cur, operator)
        if in_force is None and action != "budget":
            add("block", "manage.budget_unknown", "This campaign has no daily campaign budget on Meta now, so we "
                "can't check it against your plan's limit.", "Turn it on in Ads Manager.", "daily_budget")
        elif cap and in_force is not None and in_force > cap:
            name = plans.PLANS.get(code, {}).get("name", "Your")
            add("block", "manage.budget_ceiling",
                f"Your {name} plan runs campaigns up to {money(cap, cur)} a day from Campaignify. This one would be "
                f"{money(in_force, cur)} a day.", f"Lower the daily budget first, or email {launches.SUPPORT_EMAIL} to "
                "raise the limit.", "daily_budget")
    if action == "budget" and budget_after is not None and budget_now is not None and budget_after > budget_now:
        base = _raise_base(conn, L, budget_now)
        if base and budget_after > base * BIG_RAISE:
            add("warn", "manage.big_raise", f"That is more than 1.5 times the budget of 24 hours ago "
                f"({money(base, cur)} a day). Big changes can send the campaign back into Meta's learning phase.",
                "Tick the box to confirm it.", "daily_budget")
    if action == "on":
        for c in changes:
            eff = (state.get(c["id"]) or {}).get("effective_status") or ""
            if c["kind"] == "ad" and eff == "DISAPPROVED":
                add("warn", "manage.ad_disapproved", f"Meta didn't approve ad {c['n']}, so it won't run even when on.",
                    "See why in Ads Manager.", "ad")
        if target != "campaign" and camp is not None and \
                ((state.get(camp["external_id"]) or {}).get("status") or camp["status"]) != "ACTIVE":
            add("warn", "manage.campaign_paused", "The campaign is paused, so this ad starts only when you turn the "
                "campaign on.", "", "ad")
    if action == "off" and target != "campaign":
        live = [a for a in obj["ads"] if ((state.get(a["row"]["external_id"]) or {}).get("status") or a["row"]["status"])
                == "ACTIVE" and a["row"]["external_id"] != str(target)]
        if not live and ((state.get(camp["external_id"]) or {}).get("status") or camp["status"]) == "ACTIVE":
            add("warn", "manage.last_ad", "No other ad of this campaign is on, so it stops showing ads.", "", "ad")
    return out


def make_card(conn, L, conn_row, action, target, changes, state, issues, budget_after=None) -> dict:
    """The card: what will change on Meta, in plain words, from the exact changes apply() will make."""
    obj = objects(conn, L)
    camp = obj["campaign"]
    cur = (conn_row["currency"] if conn_row is not None else None) or "USD"
    cname = camp["name"] if camp is not None else ""
    budget_now = (state.get(camp["external_id"]) or {}).get("daily_budget") if camp is not None else None
    lines = []
    for c in changes:
        if c["field"] == "status":
            lines.append(f"{c['label']}. From {STATUS_WORDS.get(c['before'], c['before'])} to "
                         f"{STATUS_WORDS.get(c['after'], c['after'])}.")
        else:
            lines.append(f"The daily budget. From {money(c['before'], cur)} to {money(c['after'], cur)} a day.")
    title = {"on": "Turn on", "off": "Pause", "budget": "Change the daily budget of"}[action]
    ad_n = next((c["n"] for c in changes if c["kind"] == "ad" and target != "campaign"), None)
    if target != "campaign" and action != "budget":
        a = next((x for x in obj["ads"] if x["row"]["external_id"] == str(target)), None)
        ad_n = a["n"] if a else ad_n
        title += f" ad {ad_n} of {cname}"
    else:
        title += f" {cname}"
    notes = []
    if action == "on" and target == "campaign":
        notes.append(f"Meta starts showing the ads and charging your Meta payment method, up to "
                     f"{money(budget_now, cur)} a day for this campaign.")
    elif action == "on":
        notes.append("Meta starts showing this ad when the campaign is on.")
    elif action == "off" and target == "campaign":
        notes.append("Meta stops showing its ads, usually within a few minutes. What was spent stays spent.")
    elif action == "off":
        notes.append("Meta stops showing this ad. The rest of the campaign keeps running.")
    else:
        notes.append("Meta uses the new budget from now on. Meta may spend up to 75% more than the daily budget on a "
                     "good day and less on others, averaging out over the week.")
    kinds = ai_ads(conn, L)
    turning_on = [c["n"] for c in changes if c["kind"] == "ad" and c["after"] == "ACTIVE"]
    if action == "on" and target == "campaign":
        live_ads = turning_on + [a["n"] for a in obj["ads"] if
                                 ((state.get(a["row"]["external_id"]) or {}).get("status") or a["row"]["status"]) == "ACTIVE"]
    else:
        live_ads = turning_on
    ai_kinds = {n: kinds[n] for n in sorted(set(live_ads)) if n in kinds}
    ai = bool(ai_kinds) and action == "on"
    ai_text = ""
    if ai:
        ai_text = (ai_line(ai_kinds) + " Before you approve, tick Meta's AI label on "
                   + ("that ad" if len(ai_kinds) == 1 else "those ads") + " in Ads Manager. Meta has no setting "
                   "Campaignify can switch for it yet.")
    big = any(i.rule_id == "manage.big_raise" for i in issues)
    account = (f"{conn_row['account_name'] or 'Ad account'} (act_{conn_row['external_account_id']})"
               if conn_row is not None else "")
    return {"action": action, "target": target, "title": title, "lines": lines, "notes": notes,
            "changes": [dict(c, done=False) for c in changes], "currency": cur, "account": account,
            "ai": ai, "ai_text": ai_text, "ai_count": len(ai_kinds) if ai else 0, "big": big,
            "manager": launches.ads_manager_url(conn_row["external_account_id"], L["external_campaign_id"])
            if conn_row is not None else None,
            "issues": [i.as_dict() for i in issues], "blocking": adpolicy.blocking(issues),
            "budget_after": budget_after}


def _hash(changes: list) -> str:
    return hashlib.sha256(json.dumps([[c["id"], c["field"], c["before"], c["after"]] for c in changes],
                                     sort_keys=True).encode()).hexdigest()[:32]


def busy(conn, L):
    """A change of this campaign still being made (approved, not finished), or None."""
    return conn.execute("SELECT * FROM approval_cards WHERE launch_id=? AND kind IN ('status_change','budget_change') "
                        "AND decision='approved' AND apply_state IN ('queued','applying','waiting') ORDER BY id LIMIT 1",
                        (L["id"],)).fetchone()


def draft(conn, ws, user, L, action, target="campaign", budget_after=None, operator=False) -> int:
    """Store an undecided change card for `action` (on | off | budget) on `target` ("campaign" or an ad's external
    id). Reads Meta first. Returns the card id. Raises Refused when it can't be drafted."""
    if action not in ("on", "off", "budget"):
        raise Refused("Pick turn on, pause or a new budget.")
    if L["platform"] != "meta":
        raise Refused("Manage this campaign in Google Ads for now.")
    obj = objects(conn, L)
    if L["status"] != "paused" or obj["campaign"] is None:
        raise Refused("This campaign isn't on Meta yet.")
    if action == "budget":
        target = "campaign"
    elif target != "campaign" and str(target) not in {a["row"]["external_id"] for a in obj["ads"]}:
        raise Refused("That ad isn't part of this campaign.")
    if busy(conn, L) is not None:
        raise Refused("Another change to this campaign is still being made. Wait for it to finish.")
    conn_row = connection_of(conn, L)
    token = _token(conn, conn_row)
    try:
        state = read(conn, L, conn_row, token, obj)
    except adhttp.TokenInvalid:
        adplatforms.mark(conn, conn_row["id"], "expired", "Meta ended this access. Reconnect.")
        raise Refused(launches.ERRORS[(190, None)]) from None
    except adhttp.RateLimited:
        raise Refused("Meta asked us to slow down. Try again in a minute.") from None
    except adhttp.AdHttpError as e:
        raise Refused(launches.translate(e)) from None
    obj = objects(conn, L)
    if obj["campaign"] is None or obj["campaign"]["status"] == "DELETED":
        raise Refused("This campaign was deleted on Meta.")
    changes = plan_changes(conn, L, obj, state, action, target, budget_after)
    issues = rules(conn, ws, L, conn_row, action, target, changes, state, budget_after, operator)
    card = make_card(conn, L, conn_row, action, target, changes, state, issues, budget_after)
    before = {"read": state, "budget": (state.get(obj["campaign"]["external_id"]) or {}).get("daily_budget")}
    now = db.now()
    conn.execute("UPDATE approval_cards SET decision='replaced', decided_at=? WHERE launch_id=? AND kind IN "
                 "('status_change','budget_change') AND decision IS NULL", (now, L["id"]))
    card_id = conn.execute(
        "INSERT INTO approval_cards (workspace_id, launch_id, kind, campaign_id, connection_id, spec_hash, before_json, "
        "after_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (L["workspace_id"], L["id"], "budget_change" if action == "budget" else "status_change", L["plan_id"],
         L["connection_id"], _hash(changes), json.dumps(before), json.dumps(card), user["id"], now)).lastrowid
    conn.commit()
    log.info("change card %s drafted for launch %s by user %s: %s %s", card_id, L["id"], user["id"], action,
             len(changes))
    return card_id


def approve(conn, ws, user, card_row, form_hash: str, confirm_big=False, confirm_ai=False, operator=False):
    """Approve an undecided change card and start applying it. Re-reads Meta (the card must still be true) and
    re-checks the rules. Returns the apply state after the first try (done | waiting | failed). Raises NotApproved."""
    if card_row["kind"] not in KINDS:
        raise NotApproved("That isn't a change card.")
    if card_row["decision"] is not None:
        raise NotApproved("That card was already decided. Make the change again for a new card.")
    if (form_hash or "") != card_row["spec_hash"]:
        raise NotApproved("The card changed. Check it again, then approve.")
    L = launch_of(conn, ws["id"], card_row["launch_id"] or 0)
    if L is None:
        raise NotApproved("That campaign isn't in this workspace.")
    if busy(conn, L) is not None:
        raise NotApproved("Another change to this campaign is still being made. Wait for it to finish.")
    card = json.loads(card_row["after_json"])
    conn_row = connection_of(conn, L)
    try:
        token = _token(conn, conn_row)
        state = read(conn, L, conn_row, token)
    except Refused as e:
        raise NotApproved(str(e)) from None
    except adhttp.TokenInvalid:
        adplatforms.mark(conn, conn_row["id"], "expired", "Meta ended this access. Reconnect.")
        raise NotApproved(launches.ERRORS[(190, None)]) from None
    except adhttp.RateLimited:
        raise NotApproved("Meta asked us to slow down. Try again in a minute.") from None
    except adhttp.AdHttpError as e:
        raise NotApproved(launches.translate(e)) from None
    for c in card["changes"]:
        now_v = (state.get(c["id"]) or {}).get("daily_budget" if c["field"] == "daily_budget" else "status")
        if c["id"] not in state or now_v != c["before"]:
            raise NotApproved(f"{c['label']} changed on Meta since this card was made. Make the change again for a "
                              "new card.")
    issues = rules(conn, ws, L, conn_row, card["action"], card["target"], card["changes"], state,
                   card.get("budget_after"), operator)
    first = next((i for i in issues if i.level == "block"), None)
    if first is not None:
        raise NotApproved(f"{first.message} {first.fix}".strip())
    if card.get("big") and not confirm_big:
        raise NotApproved("Tick the box to confirm a raise of more than half.")
    if card.get("ai") and not confirm_ai:
        raise NotApproved("Tick the box once Meta's AI label is on in Ads Manager.")
    now = _now_iso()          # = db.now() in production; the wait limit (WAIT_MAX_S) counts from it
    n = conn.execute("UPDATE approval_cards SET decision='approved', decided_by=?, decided_at=?, apply_state='queued', "
                     "next_at=NULL, lease_at=NULL, error=NULL WHERE id=? AND decision IS NULL",
                     (user["id"], now, card_row["id"])).rowcount
    conn.commit()
    if not n:
        raise NotApproved("That card was already decided. Make the change again for a new card.")
    log.info("change card %s approved by user %s", card_row["id"], user["id"])
    return apply(card_row["id"]) or "queued"


def discard(conn, ws, user, card_row):
    conn.execute("UPDATE approval_cards SET decision='rejected', decided_by=?, decided_at=? WHERE id=? AND "
                 "workspace_id=? AND decision IS NULL", (user["id"], db.now(), card_row["id"], ws["id"]))
    conn.commit()


# ---------------------------------------------------------------- applying an approved card

def _claim(conn, card_id: int):
    now, cut = _now_iso(), _now_iso(-LEASE_S)
    n = conn.execute("UPDATE approval_cards SET apply_state='applying', lease_at=?, attempts=attempts+1 WHERE id=? AND "
                     "decision='approved' AND apply_state IN ('queued','waiting','applying') AND (next_at IS NULL OR "
                     "next_at<=?) AND (lease_at IS NULL OR lease_at<?)", (now, card_id, now, cut)).rowcount
    conn.commit()
    return conn.execute("SELECT * FROM approval_cards WHERE id=?", (card_id,)).fetchone() if n else None


def _left_words(card) -> str:
    """What is true on Meta when a change stops part way."""
    done = {c["id"] for c in card["changes"] if c.get("done")}
    camp = next((c for c in card["changes"] if c["kind"] == "campaign"), None)
    if card["action"] == "on" and card["target"] == "campaign":
        return "Nothing started: the campaign is still paused." if camp is None or camp["id"] not in done else ""
    if card["action"] == "off":
        return ("The campaign is still on. Pause it in Ads Manager if it can't wait." if card["target"] == "campaign"
                else "The ad is still on.")
    if card["action"] == "budget":
        return "The budget is unchanged."
    return "The ad is unchanged."


def _finish(conn, card_row, card, state, error=None):
    now = db.now()
    conn.execute("UPDATE approval_cards SET after_json=?, apply_state=?, error=?, applied_at=?, lease_at=NULL, "
                 "next_at=NULL WHERE id=?", (json.dumps(card), state, error, now if state == "done" else None,
                                             card_row["id"]))
    conn.commit()


def apply(card_id: int) -> str | None:
    """Make the changes of an approved card (or continue them). Returns done | waiting | failed, or None when it isn't
    due or another run holds it."""
    with db.connect() as conn:
        row = _claim(conn, card_id)
        if row is None:
            return None
        card = json.loads(row["after_json"])
        L = conn.execute("SELECT * FROM launches WHERE id=? AND workspace_id=? AND platform='meta'",
                         (row["launch_id"], row["workspace_id"])).fetchone()
        conn_row = connection_of(conn, L) if L is not None else None
        if L is None:
            _finish(conn, row, card, "failed", "That campaign isn't in this workspace any more.")
            return "failed"
        try:
            token = _token(conn, conn_row)
        except Refused as e:
            _finish(conn, row, card, "failed", f"{e} {_left_words(card)}".strip())
            return "failed"
        ad = adplatforms.adapter(L["platform"])
        account = conn_row["external_account_id"]
        for c in card["changes"]:
            if c.get("done"):
                continue
            try:
                if c["field"] == "status":
                    ad.set_status(token, account, c["id"], c["after"])
                else:
                    ad.update_budget(token, account, c["id"], int(c["after"]))
            except Exception as e:  # noqa: BLE001  sorted below; a change never stays half-claimed
                waiting = _wait(conn, row, card, e, account)
                if waiting:
                    return waiting
                if isinstance(e, adhttp.TokenInvalid):
                    adplatforms.mark(conn, conn_row["id"], "expired", "Meta ended this access. Reconnect.")
                message = (launches.WAITED_WORDS if isinstance(e, adhttp.RateLimited) else launches.translate(e))
                raw = adhttp.scrub(str(e))[:300]
                c["error"] = (f"Meta code {e.code}, subcode {e.subcode}: " if isinstance(e, adhttp.AdHttpError)
                              and e.code is not None else "") + raw.split(": ", 1)[-1]
                _finish(conn, row, card, "failed", f"{message} {_left_words(card)}".strip())
                log.warning("change card %s failed at %s: %s", row["id"], c["id"], type(e).__name__)
                return "failed"
            c["done"] = True
            if c["field"] == "status":
                conn.execute("UPDATE ad_objects SET status=?, effective_status=NULL, synced_at=NULL WHERE platform=? AND "
                             "external_id=?", (c["after"], L["platform"], c["id"]))
            else:
                conn.execute("UPDATE ad_objects SET daily_budget_minor=? WHERE platform=? AND external_id=?",
                             (int(c["after"]), L["platform"], c["id"]))
            conn.execute("UPDATE approval_cards SET after_json=? WHERE id=?", (json.dumps(card), row["id"]))
            conn.commit()
        _finish(conn, row, card, "done")
        log.info("change card %s applied: %s changes", row["id"], len(card["changes"]))
        return "done"


def _wait(conn, row, card, e, account=None) -> str | None:
    """Put the card back to wait when Meta says later (rate limit, the edit slot) or the network failed with tries
    left. Returns "waiting", or None when the change should fail."""
    waited = _age_s(row["decided_at"])
    if isinstance(e, adhttp.RateLimited):
        if waited > WAIT_MAX_S:
            return None
        gap = max(5.0, float(e.retry_after or 15.0))
        if e.code is not None and e.subcode != 4841018 and account:
            # Meta's own "slow down" for the whole ad account: every caller (launches, insights) waits too.
            # (Before this connection writes: block() writes through its own connection.)
            adhttp.block("meta", f"act_{account}", min(gap, 300.0))
        # A wait is not a failed try: nothing was changed (our budget or Meta refused before the change).
        conn.execute("UPDATE approval_cards SET attempts=MAX(0, attempts-1) WHERE id=?", (row["id"],))
    elif (isinstance(e, adhttp.NetworkError) or (isinstance(e, adhttp.AdHttpError) and not isinstance(
            e, (adhttp.TokenInvalid, adhttp.PermissionDenied)) and ((e.status or 0) >= 500 or e.code in adhttp.META_TRANSIENT))) \
            and row["attempts"] < NETWORK_TRIES:
        gap = RETRY_GAP_S * row["attempts"]
    else:
        return None
    conn.execute("UPDATE approval_cards SET after_json=?, apply_state='waiting', next_at=?, lease_at=NULL WHERE id=?",
                 (json.dumps(card), _now_iso(gap), row["id"]))
    conn.commit()
    log.info("change card %s waits %d s: %s", row["id"], int(gap), type(e).__name__)
    return "waiting"


def due(conn, limit: int = 5) -> list:
    now, cut = _now_iso(), _now_iso(-LEASE_S)
    return [(r["id"], r["workspace_id"]) for r in conn.execute(
        "SELECT id, workspace_id FROM approval_cards WHERE kind IN ('status_change','budget_change') AND "
        "decision='approved' AND apply_state IN ('queued','waiting','applying') AND (next_at IS NULL OR next_at<=?) AND "
        "(lease_at IS NULL OR lease_at<?) ORDER BY id LIMIT ?", (now, cut, limit)).fetchall()]


def tick(limit: int = 5) -> int:
    """The worker's pass: finish approved changes that wait, for workspaces that still have AD_LAUNCH."""
    n = 0
    with db.connect() as conn:
        items = due(conn, limit)
        wss = {w: conn.execute("SELECT * FROM workspaces WHERE id=?", (w,)).fetchone() for _, w in items}
    for cid, w in items:
        if not adplatforms.enabled(wss.get(w)):
            continue
        try:
            apply(cid)
            n += 1
        except Exception:  # noqa: BLE001  one bad card never stops the worker; its lease runs out and it retries
            log.exception("change card %s apply failed", cid)
    return n


def maybe_tick():
    """The worker loop's call: at most every TICK_SECONDS, never raises."""
    if time.time() - _last_tick[0] < TICK_SECONDS:
        return
    _last_tick[0] = time.time()
    try:
        tick()
    except Exception:  # noqa: BLE001
        log.exception("manage tick failed")


# ---------------------------------------------------------------- what the pages show

def _who(conn, uid) -> str:
    if not uid:
        return ""
    r = conn.execute("SELECT name, email FROM users WHERE id=?", (uid,)).fetchone()
    return ((r["name"] or r["email"]) if r else "") or ""


def card_view(conn, row) -> dict:
    a = json.loads(row["after_json"] or "{}")
    return {"id": row["id"], "kind": row["kind"], "card": a, "hash": row["spec_hash"], "decision": row["decision"],
            "state": row["apply_state"], "state_word": STATE_WORDS.get(row["apply_state"] or "", ""),
            "error": row["error"], "created_at": row["created_at"], "created_by": _who(conn, row["created_by"]),
            "decided_at": row["decided_at"], "decided_by": _who(conn, row["decided_by"]),
            "applied_at": row["applied_at"], "blocking": bool(a.get("blocking"))}


def pending(conn, L):
    """The campaign's undecided change card, or None."""
    row = conn.execute("SELECT * FROM approval_cards WHERE launch_id=? AND kind IN ('status_change','budget_change') AND "
                       "decision IS NULL ORDER BY id DESC LIMIT 1", (L["id"],)).fetchone()
    return card_view(conn, row) if row else None


def history(conn, L, limit: int = 20) -> list:
    return [card_view(conn, r) for r in conn.execute(
        "SELECT * FROM approval_cards WHERE launch_id=? AND kind IN ('status_change','budget_change') AND decision IN "
        "('approved','rejected') ORDER BY id DESC LIMIT ?", (L["id"], limit)).fetchall()]


def summary(conn, L) -> dict:
    """One launch as the Campaigns page lists it."""
    obj = objects(conn, L)
    camp = obj["campaign"]
    conn_row = connection_of(conn, L)
    try:
        spec = json.loads(L["spec_json"])
    except ValueError:
        spec = {}
    cur = (conn_row["currency"] if conn_row is not None else None) or spec.get("currency") or "USD"
    plan = conn.execute("SELECT name FROM campaigns WHERE id=? AND workspace_id=?", (L["plan_id"] or 0,
                                                                                     L["workspace_id"])).fetchone()
    meta = L["platform"] == "meta"
    pword = launches.PLATFORM_WORDS.get(L["platform"], "the platform")
    if L["status"] == "paused" and camp is not None:
        # Meta's status is read back (sync); the other platforms' campaigns are made paused and turned on there
        word, tag = status_word(camp) if meta else ("Created paused", "")
    else:
        word, tag = {"queued": ("Being created", "warn"), "launching": ("Being created", "warn"),
                     "failed": ("Launch stopped", "bad"), "rolled_back": ("Launch stopped", "bad")}.get(
            L["status"], (f"Not on {pword}", ""))
    budget = budget_of(L, camp)
    live = busy(conn, L)
    return {"launch": L, "name": (camp["name"] if camp is not None else None) or spec.get("name") or "Campaign",
            "plan": plan["name"] if plan else None, "word": word, "tag": tag, "on": camp is not None and
            camp["status"] == "ACTIVE", "manageable": meta and L["status"] == "paused" and camp is not None,
            # numbers are pulled for every platform (adinsights.py); only Meta's are changed from here
            "reported": L["status"] == "paused" and camp is not None,
            "platform": L["platform"], "platform_word": pword,
            "manager_word": launches.MANAGER_WORDS.get(L["platform"], "Ads manager"),
            "budget": budget, "budget_text": f"{money(budget, cur)} a day" if budget is not None else "",
            "currency": cur, "ads": len(obj["ads"]), "objects": obj, "conn_row": conn_row,
            "manager": launches.manager_url(L["platform"], conn_row["external_account_id"], L["external_campaign_id"])
            if conn_row is not None else None,
            "busy": card_view(conn, live) if live else None, "pending": pending(conn, L),
            "synced_at": camp["synced_at"] if camp is not None else None}


def listed(conn, ws_id: int) -> list:
    """Every launch of the workspace on every platform, newest first, as the Campaigns page lists them (only Meta's
    are manageable here; the others link to their own ads manager)."""
    return [summary(conn, L) for L in conn.execute("SELECT * FROM launches WHERE workspace_id=? ORDER BY id DESC",
                                                   (int(ws_id),)).fetchall()]

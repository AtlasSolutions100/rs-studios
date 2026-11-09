"""Google Ads conversion actions for a connected customer (roadmap M3.5, research B2), through the REST interface
of the Google Ads API (adplatforms/google.py's search and the same adhttp transport).

sync(conn, connection row) runs on Google connect and on demand (Settings > Conversion tracking):
    1. reads customer.conversion_tracking_setting: the conversion tracking id, whether conversions are managed in
       a manager account (cross-account conversion tracking: then nothing is made here, the checklist says where),
       whether the customer data terms are accepted and enhanced conversions for leads is on;
    2. lists the customer's conversion actions (GAQL FROM conversion_action) and adopts ours by name;
    3. makes the missing ones in one ConversionActionService mutate:
           lead          "Campaignify lead [cfy]"           WEBPAGE, SUBMIT_LEAD_FORM, one per click, primary
           lead_server   "Campaignify lead (server) [cfy]"  UPLOAD_CLICKS, SUBMIT_LEAD_FORM, one per click, SECONDARY:
                         the Data Manager copy of the same leads (conversions.py), kept out of bidding so a lead
                         isn't counted twice
           purchase      "Campaignify purchase [cfy]"       WEBPAGE, PURCHASE, every conversion, primary; for the
                         member's own checkout page (the snippet is shown to copy)
    4. reads their tag_snippets and keeps the Google tag id (AW-...) and each conversion label. Campaignify pages
       never paste Google's HTML: pagepx.js builds the gtag calls from the id and the label (both pattern-checked).

Enhanced conversions for web can't be switched on through the API: the Settings checklist says what to do in
Google Ads, with what the API can read (customer data terms, enhanced conversions for leads) ticked for real.

Everything here sits behind CONVERSIONS_API, AD_LAUNCH and GOOGLE_ADS. sync() never raises: it returns
{"status": "ready" | "managed_elsewhere" | "error", "words": plain words, "created": n}.
"""
import logging
import re

import adhttp
import adplatforms
import db
import secretsbox

log = logging.getLogger("campaignify.adconnect")

TAG = " [cfy]"
KINDS = {
    "lead": {"name": "Campaignify lead" + TAG, "type": "WEBPAGE", "category": "SUBMIT_LEAD_FORM",
             "countingType": "ONE_PER_CLICK", "primaryForGoal": True},
    "lead_server": {"name": "Campaignify lead (server)" + TAG, "type": "UPLOAD_CLICKS", "category": "SUBMIT_LEAD_FORM",
                    "countingType": "ONE_PER_CLICK", "primaryForGoal": False},
    "purchase": {"name": "Campaignify purchase" + TAG, "type": "WEBPAGE", "category": "PURCHASE",
                 "countingType": "MANY_PER_CLICK", "primaryForGoal": True},
}
TAG_RE = re.compile(r"\b(AW-\d{6,15})\b")
SEND_TO_RE = re.compile(r"""['"]send_to['"]\s*:\s*['"](AW-\d{6,15})/([A-Za-z0-9_-]{4,64})['"]""")
ACTION_FIELDS = ("conversion_action.id, conversion_action.name, conversion_action.resource_name, conversion_action.status, "
                 "conversion_action.type, conversion_action.category, conversion_action.primary_for_goal, "
                 "conversion_action.tag_snippets")
SETTING_FIELDS = ("customer.id, customer.currency_code, customer.conversion_tracking_setting.conversion_tracking_id, "
                  "customer.conversion_tracking_setting.cross_account_conversion_tracking_id, "
                  "customer.conversion_tracking_setting.google_ads_conversion_customer, "
                  "customer.conversion_tracking_setting.accepted_customer_data_terms, "
                  "customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled")

# The user-side steps (research B2): what the API can't switch on. (key, title, words, link)
CHECKLIST = [
    ("tag", "Google tag on your Campaignify pages", "Campaignify adds the Google tag and the lead conversion to your "
     "measured pages for you.", None),
    ("terms", "Accept the customer data terms", "In Google Ads, open Goals, then Conversions, then Settings, and "
     "accept the customer data terms.", "https://ads.google.com/aw/conversions/customersettings"),
    ("ec_web", "Turn on enhanced conversions for web", "On the same Settings page, turn on enhanced conversions and "
     "pick Google tag as the method. Campaignify's thank-you page sends the hashed email with the lead.",
     "https://support.google.com/google-ads/answer/13261987"),
    ("ec_leads", "Turn on enhanced conversions for leads", "Also on that page. It lets Google match the leads "
     "Campaignify sends from the server.", "https://ads.google.com/aw/conversions/customersettings"),
    ("dm", "Let Campaignify send leads", "Campaignify sends your leads to Google from its server. If this says to "
     "do, reconnect Google Ads and allow sending conversions.", None),
]


def _bool(v):
    return None if v is None else bool(v)


def parse_snippets(snippets) -> tuple:
    """(tag id "AW-...", label) from a conversion action's tag_snippets (WEBPAGE, HTML first), or (None, None)."""
    tag = label = None
    items = sorted(snippets or [], key=lambda s: (s.get("type") != "WEBPAGE", s.get("pageFormat") != "HTML"))
    for s in items:
        m = SEND_TO_RE.search(str(s.get("eventSnippet") or ""))
        if m:
            tag, label = m.group(1), m.group(2)
            break
    if tag is None:
        for s in items:
            m = TAG_RE.search(str(s.get("globalSiteTag") or ""))
            if m:
                tag = m.group(1)
                break
    return tag, label


def _snippet_text(snippets) -> str | None:
    """Google's own HTML for the purchase action, for the member to copy onto their checkout page (shown escaped)."""
    for s in snippets or []:
        if s.get("type") == "WEBPAGE" and s.get("pageFormat") == "HTML":
            return (str(s.get("globalSiteTag") or "").strip() + "\n" + str(s.get("eventSnippet") or "").strip())[:4000]
    return None


def setup(conn, conn_id: int):
    return conn.execute("SELECT * FROM conversion_setups WHERE connection_id=?", (int(conn_id),)).fetchone()


def actions(conn, conn_id: int) -> dict:
    return {r["kind"]: r for r in conn.execute("SELECT * FROM conversion_actions WHERE connection_id=?", (int(conn_id),))}


def ready(conn, conn_row) -> bool:
    """Does this Google connection track conversions (ours, or other primary actions the account already has)? The
    launch card's "Google bids for the conversions you track" warning clears when it does."""
    if conn_row is None:
        return False
    if conn.execute("SELECT 1 FROM conversion_actions WHERE connection_id=? AND status='ENABLED' AND kind IN "
                    "('lead','purchase') LIMIT 1", (conn_row["id"],)).fetchone():
        return True
    s = setup(conn, conn_row["id"])
    return bool(s is not None and (s["other_actions"] or 0) > 0)


def _save_setup(conn, row, **f):
    have = setup(conn, row["id"])
    cols = ("customer_id", "tag_id", "managed_by", "accepted_terms", "ec_leads", "other_actions", "purchase_snippet",
            "status", "error")
    vals = {c: f.get(c, have[c] if have is not None else None) for c in cols}
    vals["other_actions"] = vals["other_actions"] or 0
    conn.execute("INSERT INTO conversion_setups (connection_id, workspace_id, customer_id, tag_id, managed_by, accepted_terms, "
                 "ec_leads, other_actions, purchase_snippet, status, error, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(connection_id) DO UPDATE SET customer_id=excluded.customer_id, tag_id=excluded.tag_id, "
                 "managed_by=excluded.managed_by, accepted_terms=excluded.accepted_terms, ec_leads=excluded.ec_leads, "
                 "other_actions=excluded.other_actions, purchase_snippet=excluded.purchase_snippet, status=excluded.status, "
                 "error=excluded.error, checked_at=excluded.checked_at",
                 (row["id"], row["workspace_id"], *[vals[c] for c in cols], db.now()))
    conn.commit()


def _list(access, cid, login) -> list:
    from adplatforms import google
    rows = google.search(access, cid, f"SELECT {ACTION_FIELDS} FROM conversion_action WHERE "
                                      "conversion_action.status != 'REMOVED'", login=login)
    return [r.get("conversionAction") or {} for r in rows]


def create_operations(kinds, currency: str) -> list:
    """The ConversionActionService operations for `kinds` (REST field names, v25 reference read 2026-11-09)."""
    ops = []
    for k in kinds:
        spec = KINDS[k]
        c = {"name": spec["name"], "type": spec["type"], "category": spec["category"], "status": "ENABLED",
             "countingType": spec["countingType"], "primaryForGoal": spec["primaryForGoal"],
             "clickThroughLookbackWindowDays": 30,
             "attributionModelSettings": {"attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"}}
        if k == "purchase":
            c["valueSettings"] = {"defaultValue": 1.0, "defaultCurrencyCode": currency or "USD",
                                  "alwaysUseDefaultValue": False}
        ops.append({"create": c})
    return ops


def sync(conn, row, create: bool = True) -> dict:
    """Read, adopt and (with create) make this connection's conversion actions. Never raises."""
    from adplatforms import google
    if row is None or row["platform"] != "google" or row["status"] != "active":
        return {"status": "error", "words": "Connect Google Ads first.", "created": 0}
    cid, login = str(row["external_account_id"]), row["business_id"]
    try:
        access = google.access_token(adplatforms.open_token(row))
        st = google.search(access, cid, f"SELECT {SETTING_FIELDS} FROM customer LIMIT 1", login=login)
        cust = (st[0] if st else {}).get("customer") or {}
        cts = cust.get("conversionTrackingSetting") or {}
        owner_rn = str(cts.get("googleAdsConversionCustomer") or f"customers/{cid}")
        managed_by = owner_rn.split("/")[-1] if owner_rn.split("/")[-1] != cid else None
        terms, ec_leads = _bool(cts.get("acceptedCustomerDataTerms")), _bool(cts.get("enhancedConversionsForLeadsEnabled"))
        if managed_by:
            _save_setup(conn, row, customer_id=cid, managed_by=managed_by, accepted_terms=terms, ec_leads=ec_leads,
                        status="managed_elsewhere", error=None, other_actions=1)
            return {"status": "managed_elsewhere", "created": 0,
                    "words": f"This account's conversions are managed in manager account {google.dashed(managed_by)}. "
                             "Make the lead and purchase actions there."}
        found = _list(access, cid, login)
        names = {KINDS[k]["name"]: k for k in KINDS}
        ours = {names[a.get("name")]: a for a in found if a.get("name") in names}
        others = sum(1 for a in found if a.get("name") not in names and a.get("status") == "ENABLED"
                     and a.get("primaryForGoal") is not False)
        missing = [k for k in KINDS if k not in ours]
        made = 0
        if missing and create:
            try:
                res = google._call("POST", f"/customers/{cid}/conversionActions:mutate", access, login=login,
                                   body={"operations": create_operations(missing, cust.get("currencyCode") or row["currency"])},
                                   account=cid, retries=1)
                made = len(res.get("results") or [])
            except adhttp.AdHttpError as e:
                if e.code != "conversionActionError.DUPLICATE_NAME":
                    raise
            found = _list(access, cid, login)       # snippets come only from a read
            ours = {names[a.get("name")]: a for a in found if a.get("name") in names}
        now = db.now()
        tag_id = None
        for k, a in ours.items():
            tag, label = parse_snippets(a.get("tagSnippets")) if KINDS[k]["type"] == "WEBPAGE" else (None, None)
            tag_id = tag_id or tag
            conn.execute("INSERT INTO conversion_actions (workspace_id, connection_id, customer_id, kind, external_id, "
                         "resource_name, name, status, tag_id, label, created_by_us, created_at, updated_at) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id, kind) DO UPDATE SET "
                         "customer_id=excluded.customer_id, external_id=excluded.external_id, "
                         "resource_name=excluded.resource_name, name=excluded.name, status=excluded.status, "
                         "tag_id=excluded.tag_id, label=excluded.label, updated_at=excluded.updated_at",
                         (row["workspace_id"], row["id"], cid, k, str(a.get("id") or ""), a.get("resourceName"),
                          a.get("name"), a.get("status") or "ENABLED", tag, label, 1 if k in missing else 0, now, now))
        if ours:                                    # removed in Google Ads meanwhile: forget them here too
            conn.execute(f"DELETE FROM conversion_actions WHERE connection_id=? AND kind NOT IN "
                         f"({','.join('?' * len(ours))})", (row["id"], *ours.keys()))
        else:
            conn.execute("DELETE FROM conversion_actions WHERE connection_id=?", (row["id"],))
        conn.commit()
        cts_tag = str(cts.get("conversionTrackingId") or "")
        _save_setup(conn, row, customer_id=cid, tag_id=tag_id or (f"AW-{cts_tag}" if cts_tag.isdigit() else None),
                    managed_by=None, accepted_terms=terms, ec_leads=ec_leads, other_actions=others,
                    purchase_snippet=_snippet_text((ours.get("purchase") or {}).get("tagSnippets")),
                    status="ready" if len(ours) == len(KINDS) else ("partial" if ours else "none"), error=None)
        if len(ours) == len(KINDS):
            return {"status": "ready", "created": made,
                    "words": "Lead and purchase conversion actions are set up in Google Ads." if made else
                             "Conversion actions are set up in Google Ads."}
        return {"status": "error", "created": made, "words": "Some conversion actions are missing. Try again."}
    except (adhttp.AdHttpError, secretsbox.BoxError, secretsbox.NotConfigured) as e:
        words = google.translate(e) if isinstance(e, adhttp.AdHttpError) else google.RECONNECT
        log.warning("google conversion actions for connection %s: %s", row["id"], e)
        try:
            _save_setup(conn, row, customer_id=cid, status="error", error=words[:300])
        except Exception as e2:  # noqa: BLE001
            log.error("google conversion setup for connection %s not saved: %s", row["id"], type(e2).__name__)
        return {"status": "error", "created": 0, "words": words}
    except Exception as e:  # noqa: BLE001 — a connect never fails because of this
        log.error("google conversion actions for connection %s crashed: %s", row["id"], type(e).__name__)
        return {"status": "error", "created": 0, "words": "Conversion actions weren't set up. Try again in a minute."}


def checklist(conn, conn_row) -> list:
    """The Enhanced conversions checklist for Settings: [{"key", "title", "words", "link", "state"}], state
    True (done, read from the API or ours), False (not done), None (Google doesn't say: check in Google Ads)."""
    import conversions
    s = setup(conn, conn_row["id"])
    acts = actions(conn, conn_row["id"])
    state = {"tag": bool(acts.get("lead") is not None and acts["lead"]["tag_id"] and acts["lead"]["label"]),
             "terms": None if s is None or s["accepted_terms"] is None else bool(s["accepted_terms"]),
             "ec_web": None,
             "ec_leads": None if s is None or s["ec_leads"] is None else bool(s["ec_leads"]),
             "dm": conversions.DATAMANAGER_SCOPE in str(conn_row["scopes"] or "").split()}
    return [{"key": k, "title": t, "words": w, "link": link, "state": state[k]} for k, t, w, link in CHECKLIST]


def forget(conn, conn_id: int, ws_id: int):
    """A disconnected Google connection: its actions and setup go (what we read from Google)."""
    conn.execute("DELETE FROM conversion_actions WHERE connection_id=? AND workspace_id=?", (int(conn_id), int(ws_id)))
    conn.execute("DELETE FROM conversion_setups WHERE connection_id=? AND workspace_id=?", (int(conn_id), int(ws_id)))

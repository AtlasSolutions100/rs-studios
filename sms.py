"""Text messages from flows (F3, Scale): registration (toll-free verification
or A2P 10DLC), consent, quiet hours, STOP/HELP, segments and credits.

Rules (Messaging Policy, TCPA/CTIA):
- never to anyone without recorded text consent, or who replied STOP;
- only 9:00-20:00 in the contact's time zone (else the workspace's); a text due
  outside that window waits for 9:00;
- the first text to each person carries the opt-out line;
- at most 1 text a day and 4 a week per person, 100 a day per workspace;
- content rules checked again at send time.

Prices (owner decision D5): Twilio's per-segment price and the number's monthly
rent at the 2x markup; carrier per-message fees and registration fees at cost.
"""
import json
import math
import os
import re
from datetime import datetime, timedelta

import compliance
import db
import flows
import flows_engine
import plans
import smser

OPT_OUT_LINE = "Reply STOP to opt out, HELP for help. Msg & data rates may apply."
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
START_WORDS = {"START", "UNSTOP", "YES"}
HELP_WORDS = {"HELP", "INFO"}
QUIET_START, QUIET_END = 9, 20
PER_CONTACT_DAY, PER_CONTACT_WEEK, WS_DAY = 1, 4, int(os.environ.get("RS_SMS_CAP_SCALE", "100"))


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


MARKUP = _f("RS_MARKUP", "2.0")
SEGMENT_USD = _f("RS_SMS_USD_PER_SEGMENT", "0.0083")       # Twilio's US price per segment (x markup)
CARRIER_USD = _f("RS_SMS_CARRIER_USD", "0.0030")          # carrier pass-through per segment (at cost)
NUMBER_USD = {"10dlc": _f("RS_SMS_LOCAL_NUMBER_USD", "1.15"), "tollfree": _f("RS_SMS_TOLLFREE_NUMBER_USD", "2.15")}
REG_USD = {"brand_low": _f("RS_10DLC_BRAND_LOW_USD", "4"), "brand_std": _f("RS_10DLC_BRAND_STD_USD", "44"),
           "vetting": _f("RS_10DLC_VETTING_USD", "15"), "campaign_month": _f("RS_10DLC_CAMPAIGN_MONTHLY_USD", "10"),
           "tollfree": 0.0}
LOW_VOLUME = 1000   # recommend toll-free below about this many texts a month (D7)


def credits_per_segment() -> int:
    return int(math.ceil(round((SEGMENT_USD * MARKUP + CARRIER_USD) * 100, 6)))


def number_credits(path) -> int:
    return int(math.ceil(round(NUMBER_USD[path] * MARKUP * 100, 6)))


def fees(path, volume=0) -> dict:
    """What registering costs, in credits: one-time (at cost) and monthly."""
    if path == "tollfree":
        once = {"Toll-free verification": 0}
        monthly = {"Toll-free number": number_credits("tollfree")}
    else:
        brand = "brand_low" if (volume or 0) < 6000 else "brand_std"
        once = {"Brand registration" + (" (low volume)" if brand == "brand_low" else ""): int(REG_USD[brand] * 100),
                "Campaign vetting": int(REG_USD["vetting"] * 100)}
        monthly = {"Local number": number_credits("10dlc"), "Campaign (carrier fee)": int(REG_USD["campaign_month"] * 100)}
    return {"once": once, "monthly": monthly, "now": sum(once.values()) + sum(monthly.values()),
            "per_month": sum(monthly.values())}


def segments(body: str) -> int:
    return flows.sms_segments(body)[0]


# ---------------------------------------------------------------- readiness

def profile(conn, ws_id):
    return conn.execute("SELECT * FROM sms_profiles WHERE workspace_id=?", (ws_id,)).fetchone()


def sms_ready(ws) -> list[str]:
    out = []
    if not smser.configured():
        out.append("Text messaging isn't connected yet.")
    if not plans.has(ws, "flows_sms"):
        out.append(f"Texts come with {plans.required('flows_sms')['name']}.")
    if not ws["messaging_accepted_at"]:
        out.append("Accept the Messaging Policy in Settings → Sending.")
    with db.connect() as conn:
        p = profile(conn, ws["id"])
    if not p or p["status"] != "active":
        out.append("Finish registering your texting number (Settings → Sending → Text messaging).")
    elif p["notes"] == "fee_unpaid":
        out.append("The number's monthly fee couldn't be paid: top up your credits.")
    return out


# ---------------------------------------------------------------- registration

def register(conn, ws, path, user_email) -> str:
    """Charge the fees shown, buy the number, make the Messaging Service and submit the registration."""
    p = profile(conn, ws["id"])
    fields = json.loads(p["fields_json"] or "{}") if p else {}
    fee = fees(path, int(fields.get("volume") or 0))
    if fee["now"] and not db.adjust_credits(conn, ws["id"], -fee["now"], f"Texting setup ({'toll-free' if path == 'tollfree' else '10DLC'})"):
        raise smser.SmsError(f"Registering needs {fee['now']:,} credits.")
    base = flows_engine.PUBLIC_URL
    try:
        svc = smser.create_service(f"Campaignify ws {ws['id']}", f"{base}/webhooks/twilio/sms", f"{base}/webhooks/twilio/status")
        num = smser.buy_number(smser.find_number(path == "tollfree", fields.get("area_code", "")))
        smser.attach_number(svc, num["sid"])
        samples = sample_messages(conn, ws)
        upd = {"messaging_service_sid": svc, "phone_sid": num["sid"], "phone_number": num["phone_number"], "path": path,
               "number_fee_month": datetime.utcnow().strftime("%Y-%m")}
        if path == "tollfree":
            upd["verification_sid"] = smser.submit_tollfree(num["sid"], fields, samples, optin_url(conn, ws), user_email)
        else:
            upd["customer_profile_sid"] = smser.create_customer_profile(fields, user_email)
            upd["brand_type"] = "low_volume" if int(fields.get("volume") or 0) < 6000 else "standard"
    except smser.SmsError:
        if fee["now"]:
            db.adjust_credits(conn, ws["id"], fee["now"], "refund: texting setup didn't go through")
        raise
    upd.update(status="submitted", updated_at=db.now(), notes=None, fees_json=json.dumps(fee))
    conn.execute(f"UPDATE sms_profiles SET {', '.join(k + '=?' for k in upd)} WHERE workspace_id=?", (*upd.values(), ws["id"]))
    return upd["phone_number"]


def sample_messages(conn, ws) -> list[str]:
    """Up to five texts from the workspace's flows (the carrier reviews these)."""
    out = []
    for f in conn.execute("SELECT graph_json FROM flows WHERE workspace_id=? AND status<>'archived'", (ws["id"],)):
        for s, _ in flows.walk(flows.unpack(f["graph_json"])["tree"]["steps"]):
            if s["type"] == "sms" and s.get("body"):
                out.append(flows.render(s["body"], {"name": "Jamie"}, flows_engine._brand(conn, ws["id"])) + " " + OPT_OUT_LINE)
    return (out or [f"{flows_engine._brand(conn, ws['id'])['name'] or 'Our shop'}: thanks for signing up. " + OPT_OUT_LINE])[:5]


def optin_url(conn, ws) -> str:
    f = conn.execute("SELECT code FROM forms WHERE workspace_id=? AND consent_sms=1 ORDER BY id DESC LIMIT 1", (ws["id"],)).fetchone()
    return f"{flows_engine.PUBLIC_URL}/f/{f['code']}" if f else flows_engine.PUBLIC_URL


def advance_profiles(now=None) -> int:
    """Poll registrations every 30 minutes and take the next step when the last one is approved."""
    now = now or flows_engine.utcnow()
    moved = 0
    with db.connect() as conn:
        for p in conn.execute("SELECT * FROM sms_profiles WHERE status IN ('submitted','brand_pending','campaign_pending')").fetchall():
            if p["checked_at"] and flows_engine.parse(p["checked_at"]) > now - timedelta(minutes=30):
                continue
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (p["workspace_id"],)).fetchone()
            new, extra = p["status"], {}
            try:
                if p["path"] == "tollfree":
                    st = smser.tollfree_status(p["verification_sid"])
                    new = "active" if st == "TWILIO_APPROVED" else "rejected" if st == "TWILIO_REJECTED" else new
                elif p["status"] == "submitted":
                    st = smser.profile_status(p["customer_profile_sid"])
                    if st == "twilio-approved":
                        extra["brand_sid"] = smser.create_brand(p["customer_profile_sid"], p["brand_type"] == "low_volume")
                        new = "brand_pending"
                    elif st == "twilio-rejected":
                        new = "rejected"
                elif p["status"] == "brand_pending":
                    st = smser.brand_status(p["brand_sid"])
                    if st == "APPROVED":
                        f = json.loads(p["fields_json"] or "{}")
                        extra["campaign_sid"] = smser.create_campaign(p["messaging_service_sid"], p["brand_sid"], f,
                                                                      sample_messages(conn, ws))
                        new = "campaign_pending"
                    elif st == "FAILED":
                        new = "rejected"
                else:
                    st = smser.campaign_status(p["messaging_service_sid"], p["campaign_sid"])
                    new = "active" if st == "VERIFIED" else "rejected" if st == "FAILED" else new
            except smser.SmsError as e:
                extra["notes"] = str(e)[:300]
            extra.update(status=new, checked_at=flows_engine.iso(now), updated_at=db.now())
            conn.execute(f"UPDATE sms_profiles SET {', '.join(k + '=?' for k in extra)} WHERE workspace_id=?",
                         (*extra.values(), p["workspace_id"]))
            if new != p["status"]:
                moved += 1
                msg = {"active": "Your texting number is approved: SMS steps in your flows now send.",
                       "rejected": "Your texting registration was rejected. Open Settings → Sending → Text messaging.",
                       "brand_pending": "Business profile approved; your brand registration is under review.",
                       "campaign_pending": "Brand approved; your texting campaign is under review (usually 5-10 days)."}.get(new)
                if msg:
                    flows_engine._task(conn, p["workspace_id"], None, None, None, None, "notify", msg, "", now)
            conn.commit()
        _monthly_fees(conn, now)
    return moved


def _monthly_fees(conn, now):
    """On the first tick of each month: the number's rent (and 10DLC campaign fee)."""
    month = now.strftime("%Y-%m")
    label = now.strftime("%B")
    for p in conn.execute("SELECT * FROM sms_profiles WHERE phone_number IS NOT NULL AND status NOT IN ('rejected','closed') "
                          "AND COALESCE(number_fee_month,'')<>?", (month,)).fetchall():
        f = fees(p["path"], int(json.loads(p["fields_json"] or "{}").get("volume") or 0))
        ok = db.adjust_credits(conn, p["workspace_id"], -f["per_month"], f"SMS number · {label}")
        conn.execute("UPDATE sms_profiles SET number_fee_month=?, notes=? WHERE workspace_id=?",
                     (month, None if ok else "fee_unpaid", p["workspace_id"]))
        conn.commit()


# ---------------------------------------------------------------- sending

def queue_sms(conn, run, node, c, now):
    """flows_engine sender for "sms": queue when the workspace can text, else None (-> a task)."""
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (run["workspace_id"],)).fetchone()
    if sms_ready(ws):
        return None
    body = flows.render(node.get("body") or "", dict(c), flows_engine._brand(conn, ws["id"]), flows_engine._links(conn, ws["id"]))
    if conn.execute("SELECT 1 FROM messages WHERE run_id=? AND node_id=?", (run["id"], node["id"])).fetchone():
        return ("skipped", "Already queued")
    import secrets
    conn.execute("INSERT INTO messages (workspace_id, contact_id, run_id, flow_id, node_id, channel, to_addr, body_text, status, "
                 "token, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (ws["id"], c["id"], run["id"], run["flow_id"], node["id"], "sms", c["phone_e164"] or "", body[:640], "queued",
                  secrets.token_urlsafe(18), flows_engine.iso(now)))
    return ("queued", body[:80])


def _tz(conn, c, ws):
    return c["timezone"] or ws["timezone"] or flows_engine._ws_tz(conn, ws["id"])


def next_allowed(now, tzname) -> datetime | None:
    """None when it's 9:00-20:00 there now, else the next 9:00 local (in UTC)."""
    z = flows_engine._zone(tzname)
    loc = now.astimezone(z)
    if QUIET_START <= loc.hour < QUIET_END:
        return None
    nxt = loc.replace(hour=QUIET_START, minute=0, second=0, microsecond=0)
    if loc.hour >= QUIET_END:
        nxt += timedelta(days=1)
    return nxt.astimezone(flows_engine.utcnow().tzinfo)


def _count(conn, ws_id, since, contact_id=None):
    q = "SELECT COUNT(*) FROM messages WHERE workspace_id=? AND channel='sms' AND sent_at>=?"
    a = [ws_id, flows_engine.iso(since)]
    if contact_id:
        q += " AND contact_id=?"
        a.append(contact_id)
    return conn.execute(q, a).fetchone()[0]


def send_queued(now=None, limit=100) -> int:
    now = now or flows_engine.utcnow()
    sent = 0
    import messaging
    with db.connect() as conn:
        for m in conn.execute("SELECT * FROM messages WHERE status='queued' AND channel='sms' AND (scheduled_at IS NULL OR "
                              "scheduled_at<=?) ORDER BY id LIMIT ?", (flows_engine.iso(now), limit)).fetchall():
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (m["workspace_id"],)).fetchone()
            c = conn.execute("SELECT * FROM contacts WHERE id=?", (m["contact_id"],)).fetchone()
            f = conn.execute("SELECT * FROM flows WHERE id=?", (m["flow_id"],)).fetchone()
            if f is None or f["status"] != "on":
                continue
            to = (c["phone_e164"] if c else "") or ""
            if c is None:
                messaging._finish(conn, m, "skipped", now, "The contact was deleted.")
            elif c["sms_stopped_at"] or flows_engine.suppressed(conn, ws["id"], "sms", to):
                messaging._finish(conn, m, "suppressed", now, "Not sent: they replied STOP.")
            elif (c["sms_consent"] or "none") == "none" or not to:
                messaging._finish(conn, m, "skipped", now, "Not sent: no text consent or phone number on record.")
            elif not compliance.check(m["body_text"])["ok"]:
                messaging._finish(conn, m, "failed", now, "Not sent: the text breaks the content rules.")
            elif sms_ready(ws):
                continue
            elif next_allowed(now, _tz(conn, c, ws)):
                conn.execute("UPDATE messages SET scheduled_at=? WHERE id=?",
                             (flows_engine.iso(next_allowed(now, _tz(conn, c, ws))), m["id"]))
            elif _count(conn, ws["id"], now - timedelta(days=1)) >= WS_DAY:
                messaging._pause(conn, f["id"], "this workspace reached its daily text limit", now)
            elif _count(conn, ws["id"], now - timedelta(days=1), c["id"]) >= PER_CONTACT_DAY or \
                    _count(conn, ws["id"], now - timedelta(days=7), c["id"]) >= PER_CONTACT_WEEK:
                conn.execute("UPDATE messages SET scheduled_at=? WHERE id=?", (flows_engine.iso(now + timedelta(hours=24)), m["id"]))
            else:
                body = m["body_text"]
                first = not conn.execute("SELECT 1 FROM messages WHERE channel='sms' AND contact_id=? AND sent_at IS NOT NULL",
                                         (c["id"],)).fetchone()
                if first and "STOP" not in body.upper():
                    body = body.rstrip() + " " + OPT_OUT_LINE
                segs = segments(body)
                cost = segs * credits_per_segment()
                if not db.adjust_credits(conn, ws["id"], -cost, f"SMS · {segs} segment{'s' if segs != 1 else ''} · {f['name']}"):
                    messaging._finish(conn, m, "failed", now, "Not sent: out of credits.")
                    messaging._pause(conn, f["id"], "the workspace ran out of credits", now)
                else:
                    p = profile(conn, ws["id"])
                    try:
                        sid = smser.send(p["messaging_service_sid"], to, body, f"{flows_engine.PUBLIC_URL}/webhooks/twilio/status")
                        conn.execute("UPDATE messages SET body_text=?, segments=?, from_addr=? WHERE id=?",
                                     (body, segs, p["phone_number"], m["id"]))
                        messaging._finish(conn, m, "sent", now, provider_id=sid, credits=cost)
                        sent += 1
                    except smser.SmsError as e:
                        db.adjust_credits(conn, ws["id"], cost, "refund: text not sent")
                        messaging._finish(conn, m, "failed", now, f"Twilio refused it: {str(e)[:200]}")
            conn.commit()
    return sent


# ---------------------------------------------------------------- inbound

def help_text(conn, ws) -> str:
    brand = flows_engine._brand(conn, ws["id"])["name"] or ws["sender_name"] or "This sender"
    contact = (ws["reply_to"] or "").strip()
    return (f"{brand}: marketing texts. " + (f"Questions: {contact}. " if contact else "") +
            "Msg & data rates may apply. Reply STOP to opt out.")


def inbound(conn, to: str, frm: str, body: str) -> str | None:
    """A text to one of our numbers. Returns a reply for HELP (Twilio's opt-out handling confirms STOP/START)."""
    p = conn.execute("SELECT * FROM sms_profiles WHERE phone_number=?", (to,)).fetchone()
    if not p:
        return None
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (p["workspace_id"],)).fetchone()
    word = re.sub(r"[^A-Z]", "", (body or "").strip().upper().split(" ")[0] if body else "")
    now = db.now()
    contacts = conn.execute("SELECT * FROM contacts WHERE workspace_id=? AND phone_e164=?", (ws["id"], frm)).fetchall()
    if word in STOP_WORDS:
        conn.execute("INSERT OR REPLACE INTO suppressions (workspace_id, channel, address, reason, detail, at) VALUES (?,?,?,?,?,?)",
                     (ws["id"], "sms", frm, "stop", word, now))
        for c in contacts:
            conn.execute("UPDATE contacts SET sms_stopped_at=? WHERE id=?", (now, c["id"]))
            conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                         (ws["id"], c["id"], "sms_stop", frm, word, now))
        conn.execute("UPDATE messages SET status='suppressed', error='They replied STOP.' WHERE workspace_id=? AND channel='sms' "
                     "AND to_addr=? AND status='queued'", (ws["id"], frm))
        return None
    if word in START_WORDS:
        conn.execute("DELETE FROM suppressions WHERE workspace_id=? AND channel='sms' AND address=?", (ws["id"], frm))
        for c in contacts:
            conn.execute("UPDATE contacts SET sms_stopped_at=NULL WHERE id=?", (c["id"],))
        return None
    if word in HELP_WORDS:
        return help_text(conn, ws)
    for c in contacts:
        conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                     (ws["id"], c["id"], "sms_reply", frm, (body or "")[:300], now))
    return None


def replied(conn, run, node_id) -> bool:
    return conn.execute("SELECT 1 FROM contact_events WHERE contact_id=? AND kind='sms_reply' AND at>=?",
                        (run["contact_id"], run["started_at"])).fetchone() is not None


def register_hooks():
    flows_engine.SENDERS["sms"] = queue_sms
    flows_engine.TESTS_HOOKS["replied"] = replied
    flows_engine.AFTER_TICK.append(lambda now: send_queued(now))
    flows_engine.AFTER_TICK.append(lambda now: advance_profiles(now))

"""Sending messages from flows (email in F1; texts join in F3): consent,
suppressions, caps, the footer and headers the law asks for, tracked links,
credits, and what the provider's webhooks tell us afterwards.

A flow's email step queues a `messages` row (UNIQUE per run and step, so a step
is never sent twice). `send_queued()` (each flows tick) checks suppressions,
consent and caps, sends with an idempotency key, charges the credits and
records the provider id. Every check is repeated at send time, not just when
the flow was turned on.
"""
import hashlib
import html as htmllib
import json
import os
import re
import secrets
from datetime import timedelta

import compliance
import db
import flows
import flows_engine
import mailer
import plans

EMAIL_CREDITS = int(os.environ.get("RS_EMAIL_CREDITS", "1"))         # Resend ~$0.0004-0.0009 an email, x2 (D5), min 1
CAPS = {"growth": int(os.environ.get("RS_EMAIL_CAP_GROWTH", "300")), "scale": int(os.environ.get("RS_EMAIL_CAP_SCALE", "2000"))}
WARMUP_DAYS, WARMUP_CAP = 7, int(os.environ.get("RS_EMAIL_WARMUP_CAP", "50"))
PER_CONTACT_DAY = 2
COMPLAINT_RATE, BOUNCE_RATE, RATE_WINDOW = 0.003, 0.05, 200
CAMPAIGNIFY_ADDRESS = os.environ.get("RS_POSTAL_ADDRESS", "30 N Gould St Ste R, Sheridan, WY 82801")
CONSENTED = ("form", "import", "manual")
POLICY_VERSION = "2026-10"


def public_url() -> str:
    return flows_engine.PUBLIC_URL


# ---------------------------------------------------------------- readiness

def email_ready(ws) -> list[str]:
    """What still stops this workspace's flows from actually sending email ([] = ready)."""
    out = []
    if not mailer.configured():
        out.append("Email sending isn't connected yet.")
    if not plans.has(ws, "flows_email"):
        out.append(f"Sending email from flows comes with {plans.required('flows_email')['name']}.")
    if not (ws["postal_address"] or "").strip():
        out.append("Add your postal address (it goes in every email's footer, as the law asks).")
    if not ws["messaging_accepted_at"]:
        out.append("Accept the Messaging Policy in Settings → Sending.")
    return out


def consent_ok(ws, c) -> bool:
    if (c["email_consent"] or "none") in CONSENTED:
        return True
    return bool(ws["consent_manual"]) and (c["source"] or "") == "added by hand"


def suppress(conn, ws_id, channel, address, reason, detail=""):
    conn.execute("INSERT OR REPLACE INTO suppressions (workspace_id, channel, address, reason, detail, at) VALUES (?,?,?,?,?,?)",
                 (ws_id, channel, (address or "").lower(), reason, detail[:300], db.now()))


# ---------------------------------------------------------------- queueing (from the flow step)

def queue_email(conn, run, node, c, now):
    """flows_engine sender for "email": queue the message when this workspace can send, else None (-> a task)."""
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (run["workspace_id"],)).fetchone()
    if email_ready(ws):
        return None
    brand = flows_engine._brand(conn, ws["id"])
    links = flows_engine._links(conn, ws["id"])
    subject = flows.render(node.get("subject") or "", dict(c), brand, links)
    body = flows.render(node.get("body") or "", dict(c), brand, links)
    if conn.execute("SELECT 1 FROM messages WHERE run_id=? AND node_id=?", (run["id"], node["id"])).fetchone():
        return ("skipped", "Already queued")
    conn.execute("INSERT INTO messages (workspace_id, contact_id, run_id, flow_id, node_id, channel, to_addr, subject, body_text, "
                 "status, token, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ws["id"], c["id"], run["id"], run["flow_id"], node["id"], "email", c["email"], subject[:200], body[:20000],
                  "queued", secrets.token_urlsafe(18), flows_engine.iso(now)))
    return ("queued", subject)


# ---------------------------------------------------------------- building the email

URL_RX = re.compile(r"https?://[^\s<>\"')\]]+")


def _track(conn, msg_id, text) -> str:
    def sub(m):
        url = m.group(0).rstrip(".,;:!?")
        tail = m.group(0)[len(url):]
        tok = secrets.token_urlsafe(12)
        conn.execute("INSERT INTO message_links (message_id, token, url) VALUES (?,?,?)", (msg_id, tok, url))
        return f"{public_url()}/t/{tok}" + tail
    return URL_RX.sub(sub, text)


def build(conn, ws, msg) -> dict:
    """{from, reply_to, subject, html, text, headers}: tracked links, the footer and List-Unsubscribe."""
    brand = flows_engine._brand(conn, ws["id"])
    sender = (ws["sender_name"] or brand["name"] or ws["name"] or "").strip()
    custom = None
    if "sender_domains" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        d = conn.execute("SELECT * FROM sender_domains WHERE workspace_id=? AND status='verified' ORDER BY id DESC LIMIT 1",
                         (ws["id"],)).fetchone()
        if d and plans.has(ws, "flows_email"):
            custom = f"{(d['local_part'] or 'hello')}@{d['domain']}"
    unsub = f"{public_url()}/u/{msg['token']}"
    text = _track(conn, msg["id"], msg["body_text"])
    footer_txt = f"\n\n--\n{sender}\n{ws['postal_address'].strip()}\nUnsubscribe: {unsub}"
    paras = "".join(f"<p style=\"margin:0 0 14px\">{_link_html(htmllib.escape(p)).replace(chr(10), '<br>')}</p>"
                    for p in text.split("\n\n") if p.strip())
    html = (f"<div style=\"font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:560px\">{paras}"
            f"<hr style=\"border:0;border-top:1px solid #e5e5e5;margin:24px 0 12px\"><p style=\"font-size:12px;color:#777;margin:0\">"
            f"{htmllib.escape(sender)} · {htmllib.escape(ws['postal_address'].strip())}<br>You're getting this because you "
            f"signed up with {htmllib.escape(sender or 'us')}. <a href=\"{unsub}\" style=\"color:#777\">Unsubscribe</a></p>"
            f"<img src=\"{public_url()}/t/o/{msg['token']}.gif\" width=\"1\" height=\"1\" alt=\"\" style=\"display:block\"></div>")
    return {"from": mailer.from_address(ws["id"], sender, custom),
            "reply_to": (ws["reply_to"] or "").strip() or _owner_email(conn, ws["id"]),
            "subject": msg["subject"], "html": html, "text": text + footer_txt,
            "headers": {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}


def _link_html(escaped: str) -> str:
    return re.sub(r"(https?://[^\s<&]+)", r'<a href="\1">\1</a>', escaped)


def _owner_email(conn, ws_id) -> str:
    r = conn.execute("SELECT email FROM users WHERE workspace_id=? AND role='owner' ORDER BY id LIMIT 1", (ws_id,)).fetchone()
    return r["email"] if r else ""


# ---------------------------------------------------------------- sending

def _sent_since(conn, ws_id, since, contact_id=None) -> int:
    q = "SELECT COUNT(*) FROM messages WHERE workspace_id=? AND channel='email' AND sent_at>=?"
    args = [ws_id, flows_engine.iso(since)]
    if contact_id:
        q += " AND contact_id=?"
        args.append(contact_id)
    return conn.execute(q, args).fetchone()[0]


def daily_cap(conn, ws, now) -> int:
    cap = CAPS.get(plans.plan_code(ws), CAPS["growth"])
    first = conn.execute("SELECT MIN(sent_at) FROM messages WHERE workspace_id=? AND channel='email' AND sent_at IS NOT NULL",
                         (ws["id"],)).fetchone()[0]
    if not first or flows_engine.parse(first) > now - timedelta(days=WARMUP_DAYS):
        cap = min(cap, WARMUP_CAP)   # warm-up: a new sender starts small
    return cap


def _pause(conn, flow_id, why, now):
    f = conn.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
    if f and f["status"] == "on":
        conn.execute("UPDATE flows SET status='paused' WHERE id=?", (flow_id,))
        flows_engine._task(conn, f["workspace_id"], None, None, flow_id, None, "notify", f"“{f['name']}” was paused: {why}",
                           "Open the flow to see what happened, then turn it on again.", now)


def _finish(conn, msg, status, now, error=None, provider_id=None, credits=0):
    conn.execute("UPDATE messages SET status=?, error=?, provider_id=COALESCE(?, provider_id), credits=?, sent_at=? WHERE id=?",
                 (status, error, provider_id, credits, flows_engine.iso(now) if status == "sent" else None, msg["id"]))
    kind = {"sent": "sent", "suppressed": "skipped", "skipped": "skipped", "failed": "error"}[status]
    flows_engine._event(conn, msg["flow_id"], msg["workspace_id"], msg["run_id"], msg["contact_id"], msg["node_id"], kind,
                        error or f"Email sent: {msg['subject']}", now)


def send_queued(now=None, limit=100) -> int:
    """Send what flows queued: suppression, consent, caps, content rules, then the provider. Returns how many went."""
    now = now or flows_engine.utcnow()
    sent = 0
    with db.connect() as conn:
        for msg in conn.execute("SELECT * FROM messages WHERE status='queued' AND channel='email' ORDER BY id LIMIT ?",
                                (limit,)).fetchall():
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (msg["workspace_id"],)).fetchone()
            c = conn.execute("SELECT * FROM contacts WHERE id=?", (msg["contact_id"],)).fetchone()
            f = conn.execute("SELECT * FROM flows WHERE id=?", (msg["flow_id"],)).fetchone()
            if f is None or f["status"] != "on":
                continue   # paused: stays queued until the flow is back on
            if c is None:
                _finish(conn, msg, "skipped", now, "The contact was deleted.")
            elif c["unsubscribed_at"] or flows_engine.suppressed(conn, ws["id"], "email", c["email"]):
                _finish(conn, msg, "suppressed", now, "Not sent: they unsubscribed or their address bounced.")
            elif not consent_ok(ws, c):
                _finish(conn, msg, "skipped", now, "Not sent: no email consent on record for this contact.")
            elif not compliance.check(msg["subject"], msg["body_text"])["ok"]:
                _finish(conn, msg, "failed", now, "Not sent: the email breaks the content rules.")
            elif email_ready(ws):
                continue
            elif _sent_since(conn, ws["id"], now - timedelta(days=1)) >= daily_cap(conn, ws, now):
                _pause(conn, f["id"], "this workspace reached its daily email limit", now)
                conn.commit()
                continue
            elif _sent_since(conn, ws["id"], now - timedelta(days=1), c["id"]) >= PER_CONTACT_DAY:
                continue   # at most two emails a person a day: it stays queued and goes later
            elif not db.adjust_credits(conn, ws["id"], -EMAIL_CREDITS, f"Email · {f['name']}"):
                _finish(conn, msg, "failed", now, "Not sent: out of credits.")
                _pause(conn, f["id"], "the workspace ran out of credits", now)
            else:
                try:
                    e = build(conn, ws, msg)
                    pid = mailer.send(frm=e["from"], to=c["email"], subject=e["subject"], html=e["html"], text=e["text"],
                                      reply_to=e["reply_to"], headers=e["headers"], idempotency_key=f"cfy-msg-{msg['id']}",
                                      tags={"workspace": ws["id"], "message": msg["id"]})
                    conn.execute("UPDATE messages SET from_addr=?, reply_to=?, body_html=? WHERE id=?",
                                 (e["from"], e["reply_to"], e["html"], msg["id"]))
                    _finish(conn, msg, "sent", now, provider_id=pid, credits=EMAIL_CREDITS)
                    sent += 1
                except mailer.MailError as err:
                    db.adjust_credits(conn, ws["id"], EMAIL_CREDITS, f"refund: email not sent ({str(err)[:40]})")
                    _finish(conn, msg, "failed", now, f"The email service refused it: {str(err)[:200]}")
            conn.commit()
    return sent


# ---------------------------------------------------------------- tracking and webhooks

def clicked(conn, run, node_id) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE run_id=? AND node_id=? AND clicked_at IS NOT NULL",
                        (run["id"], node_id)).fetchone() is not None


def opened(conn, run, node_id) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE run_id=? AND node_id=? AND (opened_at IS NOT NULL OR clicked_at IS NOT "
                        "NULL)", (run["id"], node_id)).fetchone() is not None


def unsubscribe(conn, msg, reason="unsubscribe", detail=""):
    """One click, no login: suppress the address in this workspace and take them out of its flows."""
    c = conn.execute("SELECT * FROM contacts WHERE id=?", (msg["contact_id"],)).fetchone()
    if not c:
        return
    now = db.now()
    suppress(conn, msg["workspace_id"], "email", c["email"], reason, detail)
    conn.execute("UPDATE contacts SET unsubscribed_at=COALESCE(unsubscribed_at, ?) WHERE id=?", (now, c["id"]))
    conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                 (msg["workspace_id"], c["id"], "unsubscribe", str(msg["id"]), detail or reason, now))
    for r in conn.execute("SELECT id FROM flow_runs WHERE workspace_id=? AND contact_id=? AND status='waiting'",
                          (msg["workspace_id"], c["id"])).fetchall():
        flows_engine.exit_run(conn, r["id"], msg["workspace_id"], "Unsubscribed")


def handle_event(conn, event: dict) -> str:
    """One Resend webhook event (already verified and de-duplicated)."""
    typ = event.get("type") or ""
    data = event.get("data") or {}
    msg = conn.execute("SELECT * FROM messages WHERE provider_id=?", (data.get("email_id") or "",)).fetchone()
    if not msg:
        return "unknown message"
    now = db.now()
    if typ == "email.delivered":
        conn.execute("UPDATE messages SET status=CASE WHEN status='sent' THEN 'delivered' ELSE status END, delivered_at=? "
                     "WHERE id=?", (now, msg["id"]))
    elif typ == "email.opened":
        conn.execute("UPDATE messages SET opened_at=COALESCE(opened_at, ?) WHERE id=?", (now, msg["id"]))
    elif typ == "email.clicked":
        conn.execute("UPDATE messages SET clicked_at=COALESCE(clicked_at, ?) WHERE id=?", (now, msg["id"]))
    elif typ == "email.bounced":
        hard = (data.get("bounce") or {}).get("type", "Permanent") != "Transient"
        conn.execute("UPDATE messages SET status='bounced' WHERE id=?", (msg["id"],))
        if hard:
            c = conn.execute("SELECT * FROM contacts WHERE id=?", (msg["contact_id"],)).fetchone()
            if c:
                suppress(conn, msg["workspace_id"], "email", c["email"], "bounce", "hard bounce")
                conn.execute("UPDATE contacts SET email_consent='none', notes=COALESCE(notes,'') || ? WHERE id=?",
                             (f"\n[{now[:10]}] Email bounced; sending stopped.", c["id"]))
        _rates(conn, msg["flow_id"])
    elif typ == "email.complained":
        conn.execute("UPDATE messages SET status='complained' WHERE id=?", (msg["id"],))
        unsubscribe(conn, msg, "complaint", "marked as spam")
        _rates(conn, msg["flow_id"])
    return typ


def _rates(conn, flow_id):
    """Auto-pause a flow whose recent emails draw complaints (>0.3%) or bounce (>5%)."""
    rows = conn.execute("SELECT status FROM messages WHERE flow_id=? AND channel='email' AND sent_at IS NOT NULL "
                        "ORDER BY id DESC LIMIT ?", (flow_id, RATE_WINDOW)).fetchall()
    n = len(rows)
    if not n:
        return
    comp = sum(1 for r in rows if r["status"] == "complained")
    bnc = sum(1 for r in rows if r["status"] == "bounced")
    if comp / n > COMPLAINT_RATE or (n >= 20 and bnc / n > BOUNCE_RATE):
        _pause(conn, flow_id, "too many spam complaints or bounces", flows_engine.utcnow())


# ---------------------------------------------------------------- consent from forms

DEFAULT_EMAIL_CONSENT = "Yes, email me about {brand}. I can unsubscribe anytime."
DEFAULT_SMS_CONSENT = ("Yes, text me about {brand}: recurring automated marketing texts at the number I gave. Consent is not "
                       "a condition of purchase. Message frequency varies. Msg & data rates may apply. Reply HELP for help, "
                       "STOP to opt out. See our Terms and Privacy.")


def consent_detail(form_id, text, ip, ua) -> str:
    return json.dumps({"form": form_id, "text_sha256": hashlib.sha256((text or "").encode()).hexdigest()[:32],
                       "ip_sha256": hashlib.sha256(("cfy:" + (ip or "")).encode()).hexdigest()[:24], "ua": (ua or "")[:160]})


def e164(phone: str, default_cc: str = "1") -> str | None:
    """+15551234567 from what people type (US/Canada numbers without a country code get +1)."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return f"+{default_cc}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def register():
    """Plug email into the flows engine (always; queue_email itself checks the workspace can send)."""
    flows_engine.SENDERS["email"] = queue_email
    flows_engine.TESTS_HOOKS["clicked"] = clicked
    flows_engine.TESTS_HOOKS["opened"] = opened
    flows_engine.AFTER_TICK.append(lambda now: send_queued(now))

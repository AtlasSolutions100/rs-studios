"""Email sending through Resend (plain HTTPS, no SDK), and its webhooks.

Shared sending domain (owner decision D6): mail from a workspace goes out as
"{Brand} via Campaignify" <ws-{id}@RS_MAIL_DOMAIN> with Reply-To set to the
customer. A workspace on Growth+ can verify its own domain (Phase 8,
sender_domains); then the From is its own.

Nothing here runs until RESEND_API_KEY and RS_MAIL_DOMAIN are set; webhooks
need RESEND_WEBHOOK_SECRET (svix signing). Tests replace `requests.post`.
"""
import base64
import hashlib
import hmac
import os
import time

import requests

API = os.environ.get("RESEND_API_BASE", "https://api.resend.com").rstrip("/")
TIMEOUT = 20


class MailError(RuntimeError):
    pass


def api_key() -> str:
    return (os.environ.get("RESEND_API_KEY") or "").strip()


def domain() -> str:
    return (os.environ.get("RS_MAIL_DOMAIN") or "").strip().lower()


def configured() -> bool:
    return bool(api_key() and domain())


def webhook_secret() -> str:
    return (os.environ.get("RESEND_WEBHOOK_SECRET") or "").strip()


def _headers(idempotency_key: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json", "User-Agent": "campaignify/1.0"}
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key[:256]
    return h


def from_address(ws_id: int, sender_name: str, custom: str | None = None) -> str:
    """The From header: the workspace's own verified address, else the shared domain."""
    name = " ".join((sender_name or "").replace('"', "").split())[:60] or "Campaignify"
    if custom:
        return f'"{name}" <{custom}>'
    return f'"{name} via Campaignify" <ws-{ws_id}@{domain()}>'


def send(*, frm: str, to: str, subject: str, html: str, text: str, reply_to: str | None = None,
         headers: dict | None = None, idempotency_key: str | None = None, tags: dict | None = None) -> str:
    """One email. Returns the provider's message id; raises MailError."""
    if not configured():
        raise MailError("Email sending isn't connected.")
    body = {"from": frm, "to": [to], "subject": subject, "html": html, "text": text, "headers": headers or {}}
    if reply_to:
        body["reply_to"] = reply_to
    if tags:
        body["tags"] = [{"name": k, "value": str(v)[:256]} for k, v in tags.items()]
    try:
        r = requests.post(f"{API}/emails", json=body, headers=_headers(idempotency_key), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise MailError(f"Couldn't reach the email service: {e}") from e
    if r.status_code >= 400:
        raise MailError(f"{r.status_code}: {r.text[:200]}")
    return (r.json() or {}).get("id") or ""


# ---------------------------------------------------------------- domains (Phase 8)

def add_domain(name: str) -> dict:
    r = requests.post(f"{API}/domains", json={"name": name}, headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise MailError(f"{r.status_code}: {r.text[:200]}")
    return r.json() or {}


def verify_domain(domain_id: str) -> dict:
    r = requests.post(f"{API}/domains/{domain_id}/verify", json={}, headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise MailError(f"{r.status_code}: {r.text[:200]}")
    return get_domain(domain_id)


def get_domain(domain_id: str) -> dict:
    r = requests.get(f"{API}/domains/{domain_id}", headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise MailError(f"{r.status_code}: {r.text[:200]}")
    return r.json() or {}


# ---------------------------------------------------------------- webhooks (svix)

def verify_webhook(headers, raw: bytes, tolerance: int = 300, now: float | None = None) -> bool:
    """Resend signs webhooks the svix way: base64 HMAC-SHA256 of "id.timestamp.body"
    with the secret after its "whsec_" prefix; svix-signature lists "v1,<sig>" values."""
    secret = webhook_secret()
    msg_id, ts, sigs = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature") or ""
    if not (secret and msg_id and ts and sigs):
        return False
    try:
        if abs((now or time.time()) - int(ts)) > tolerance:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except (ValueError, TypeError):
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, s.split(",", 1)[1]) for s in sigs.split() if s.startswith("v1,"))


def sign_webhook(secret: str, msg_id: str, ts: str, raw: bytes) -> str:
    """For tests: the svix-signature header for a body."""
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    return "v1," + base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()

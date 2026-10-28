"""Credit packs on Stripe (the Rector & Stull account).

Plain HTTPS to the Stripe API, no SDK: one call to start a Checkout Session and
a signature check for the webhook. Settings:

    STRIPE_SECRET_KEY       secret or restricted key (needs Checkout Sessions: write)
    STRIPE_WEBHOOK_SECRET   signing secret of the campaignify.ai/webhooks/stripe endpoint

Until both are set the app shows prices but no working buy buttons.

The Stripe account is shared with other Rector & Stull apps (Crowdcut, The
Morning Upgrade), so every session we start carries metadata app=campaignify
and our own purchase id, and the webhook ignores everything else. We never
create Stripe customers and never set metadata.account_id, which Crowdcut's
webhook keys on.
"""
import hashlib
import hmac
import json
import os
import time

import requests

API = "https://api.stripe.com/v1"
APP_TAG = "campaignify"
TOLERANCE = 300  # seconds a webhook signature stays valid


class BillingError(RuntimeError):
    pass


class SignatureError(ValueError):
    pass


def secret_key() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def webhook_secret() -> str:
    return (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()


def configured() -> bool:
    """Checkout works only when we can both take the payment and hear about it."""
    return bool(secret_key() and webhook_secret())


def create_checkout_session(*, price_id: str, quantity: int = 1, success_url: str, cancel_url: str,
                            email: str | None, metadata: dict, client_reference_id: str,
                            description: str, idempotency_key: str) -> dict:
    """Start a one-time Checkout Session. Returns {"id", "url"}."""
    if not secret_key():
        raise BillingError("Stripe is not configured.")
    form = [("mode", "payment"),
            ("line_items[0][price]", price_id), ("line_items[0][quantity]", str(quantity)),
            ("success_url", success_url), ("cancel_url", cancel_url),
            ("client_reference_id", client_reference_id),
            ("submit_type", "pay"),
            ("payment_intent_data[description]", description)]
    if email:
        form.append(("customer_email", email))
    for k, v in metadata.items():
        form.append((f"metadata[{k}]", str(v)))
        form.append((f"payment_intent_data[metadata][{k}]", str(v)))
    try:
        r = requests.post(f"{API}/checkout/sessions", data=form, auth=(secret_key(), ""), timeout=30,
                          headers={"Idempotency-Key": idempotency_key})
    except requests.RequestException as e:
        raise BillingError(f"Stripe unreachable: {e.__class__.__name__}") from e
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            msg = r.text
        raise BillingError(f"Stripe {r.status_code}: {str(msg)[:300]}")
    j = r.json()
    return {"id": j["id"], "url": j["url"]}


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """A Stripe-Signature header value for `payload` (the tests use this)."""
    t = int(timestamp if timestamp is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_event(payload: bytes, sig_header: str, secret: str | None = None, tolerance: int = TOLERANCE) -> dict:
    """Check the Stripe-Signature header and return the parsed event."""
    secret = secret if secret is not None else webhook_secret()
    if not secret:
        raise SignatureError("webhook secret not configured")
    parts = {}
    for item in (sig_header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        t = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        raise SignatureError("no timestamp") from None
    if abs(time.time() - t) > tolerance:
        raise SignatureError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in parts.get("v1", [])):
        raise SignatureError("signature mismatch")
    try:
        return json.loads(payload)
    except ValueError:
        raise SignatureError("body is not JSON") from None

"""Credit packs and monthly plans on Stripe (the shared operator account).

Plain HTTPS to the Stripe API, no SDK: Checkout Sessions (mode=payment for
packs, mode=subscription for plans), Customer Portal sessions, and a signature
check for the webhook. Settings:

    STRIPE_SECRET_KEY       secret or restricted key (needs Checkout Sessions: write
                            and Customer portal: write)
    STRIPE_WEBHOOK_SECRET   signing secret of the campaignify.ai/webhooks/stripe
                            endpoint (several, comma-separated, are accepted)
    STRIPE_PORTAL_CONFIG    optional: the Customer Portal configuration to use
                            (default: the "Campaignify" configuration)

Until the key and secret are set the app shows prices but no working buy buttons.

The Stripe account is shared with other apps, so every session and
subscription we start carries metadata app=campaignify and our own ids, and the
webhook ignores everything else. Plan checkouts let Stripe create the customer
(subscriptions need one); we never set metadata.account_id, which another app's
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
# The Customer Portal configuration made for Campaignify: our three plans only,
# upgrades invoiced right away, downgrades and cancellations at period end.
PORTAL_CONFIG = os.environ.get("STRIPE_PORTAL_CONFIG", "bpc_1UVhC6RskSkrZOvOuWBouu50")


class BillingError(RuntimeError):
    pass


class SignatureError(ValueError):
    pass


def secret_key() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def webhook_secret() -> str:
    return (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()


def webhook_secrets() -> list[str]:
    return [x.strip() for x in webhook_secret().split(",") if x.strip()]


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
    j = _post("/checkout/sessions", form, idempotency_key)
    return {"id": j["id"], "url": j["url"]}


def create_subscription_session(*, price_id: str, success_url: str, cancel_url: str, email: str | None,
                                customer: str | None, metadata: dict, client_reference_id: str,
                                idempotency_key: str) -> dict:
    """Start a subscription Checkout Session for one plan. The metadata goes on
    both the session and the subscription, so every invoice and subscription
    event carries it. Returns {"id", "url"}."""
    if not secret_key():
        raise BillingError("Stripe is not configured.")
    form = [("mode", "subscription"),
            ("line_items[0][price]", price_id), ("line_items[0][quantity]", "1"),
            ("success_url", success_url), ("cancel_url", cancel_url),
            ("client_reference_id", client_reference_id),
            ("allow_promotion_codes", "false")]
    if customer:
        form.append(("customer", customer))
    elif email:
        form.append(("customer_email", email))
    for k, v in metadata.items():
        form.append((f"metadata[{k}]", str(v)))
        form.append((f"subscription_data[metadata][{k}]", str(v)))
    j = _post("/checkout/sessions", form, idempotency_key)
    return {"id": j["id"], "url": j["url"]}


def create_portal_session(*, customer: str, return_url: str) -> str:
    """A Customer Portal link for managing or canceling a plan. Returns its URL."""
    if not secret_key():
        raise BillingError("Stripe is not configured.")
    form = [("customer", customer), ("return_url", return_url)]
    if PORTAL_CONFIG:
        form.append(("configuration", PORTAL_CONFIG))
    return _post("/billing_portal/sessions", form)["url"]


def _post(path: str, form: list, idempotency_key: str | None = None) -> dict:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    try:
        r = requests.post(f"{API}{path}", data=form, auth=(secret_key(), ""), timeout=30, headers=headers)
    except requests.RequestException as e:
        raise BillingError(f"Stripe unreachable: {e.__class__.__name__}") from e
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            msg = r.text
        raise BillingError(f"Stripe {r.status_code}: {str(msg)[:300]}")
    return r.json()


# ---------------------------------------------------------------- reading events
# Stripe moved some invoice fields in newer API versions (invoice.subscription
# -> invoice.parent.subscription_details.subscription, line.price ->
# line.pricing.price_details.price). These read both shapes.

def invoice_subscription(inv: dict) -> tuple[str | None, dict]:
    """(subscription id, subscription metadata) of an invoice."""
    parent = (inv.get("parent") or {}).get("subscription_details") or {}
    old = inv.get("subscription_details") or {}
    sub = parent.get("subscription") or inv.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    meta = dict(old.get("metadata") or {})
    meta.update(parent.get("metadata") or {})
    if not meta:
        for line in (inv.get("lines") or {}).get("data") or []:
            if (line.get("metadata") or {}).get("app"):
                meta = dict(line["metadata"])
                break
    return sub, meta


def invoice_lines(inv: dict) -> list[dict]:
    """[{price, amount, period_end}] for each invoice line."""
    out = []
    for line in (inv.get("lines") or {}).get("data") or []:
        price = line.get("price") or {}
        price_id = price.get("id") if isinstance(price, dict) else price
        pd = ((line.get("pricing") or {}).get("price_details") or {})
        price_id = price_id or pd.get("price")
        out.append({"price": price_id, "amount": line.get("amount") or 0,
                    "period_end": (line.get("period") or {}).get("end")})
    return out


def subscription_prices(sub: dict) -> list[str]:
    out = []
    for item in (sub.get("items") or {}).get("data") or []:
        price = item.get("price") or {}
        out.append(price.get("id") if isinstance(price, dict) else price)
    return [p for p in out if p]


def subscription_period_end(sub: dict):
    if sub.get("current_period_end"):
        return sub["current_period_end"]
    for item in (sub.get("items") or {}).get("data") or []:
        if item.get("current_period_end"):
            return item["current_period_end"]
    return None


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """A Stripe-Signature header value for `payload` (the tests use this)."""
    t = int(timestamp if timestamp is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_event(payload: bytes, sig_header: str, secret: str | None = None, tolerance: int = TOLERANCE) -> dict:
    """Check the Stripe-Signature header and return the parsed event."""
    secrets_ = [secret] if secret is not None else webhook_secrets()
    secrets_ = [x for x in secrets_ if x]
    if not secrets_:
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
    expected = [hmac.new(sec.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest() for sec in secrets_]
    if not any(hmac.compare_digest(e, v) for e in expected for v in parts.get("v1", [])):
        raise SignatureError("signature mismatch")
    try:
        return json.loads(payload)
    except ValueError:
        raise SignatureError("body is not JSON") from None

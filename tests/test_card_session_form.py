"""The card step's Stripe Checkout request (billing.create_setup_session) as sent.

Stripe API 2026-09-30 refuses `payment_method_types` on Checkout Sessions ("no longer
supported ... managed from your Dashboard settings"), so every card-step checkout failed
after the customer was made. Setup mode without it needs a currency.

    python tests/test_card_session_form.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake")
import billing  # noqa: E402

sent = {}


def fake_post(path, form, idem=None):
    sent["path"], sent["form"], sent["idem"] = path, list(form), idem
    return {"id": "cs_test_1", "url": "https://checkout.stripe.test/cs_test_1"}


billing._post = fake_post
if not billing.secret_key():
    billing.secret_key = lambda: "sk_test_fake"
r = billing.create_setup_session(customer="cus_1", success_url="https://x/done?session_id={CHECKOUT_SESSION_ID}",
                                 cancel_url="https://x/card", metadata={"app": "campaignify", "workspace_id": 7},
                                 client_reference_id="cfy-card-7", idempotency_key="k1")
keys = [k for k, _ in sent["form"]]
checks = [
    (sent["path"] == "/checkout/sessions" and r["url"].startswith("https://"), "posts one Checkout Session"),
    (("mode", "setup") in sent["form"], "setup mode"),
    (not any(k.startswith("payment_method_types") for k in keys), "no payment_method_types (refused by Stripe 2026-09-30)"),
    (("currency", "usd") in sent["form"], "a currency, which setup mode needs without payment_method_types"),
    (("customer", "cus_1") in sent["form"] and sent["idem"] == "k1", "the customer and the idempotency key"),
]
for ok, msg in checks:
    if not ok:
        raise SystemExit(f"FAIL: {msg}: {sent}")
    print(f"  ok  {msg}")
print(f"all {len(checks)} checks passed")

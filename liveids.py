"""Campaignify's own live identifiers, in one place (roadmap M7.4, observation 10; SEC-9).

Every id that ties this repo to the production accounts (the Stripe prices and Billing Portal configuration, the
Google tag and sign-in client, the Twilio messaging policies, the IndexNow key, the postal address in the email
footer) is a setting: an environment variable from settings.txt, which deploy.yml fills from the GitHub variable
of the same name when it is set. Modules read them with get(name), never from their own constants.

PRODUCTION holds today's values. They are the fallback for a setting that isn't set, so a server that has never
had these settings keeps working exactly as before. RS_LIVE_IDS=settings turns the fallback off: only settings
count and an unset id is missing (a fork, a Stripe test-mode account, a staging server must not reach the
production accounts by accident). None of these is a secret: price ids, the client id, the tag id and the IndexNow
key are sent to browsers or third parties anyway.

Not here, on purpose:
  * owner emails and owner setup codes (SEC-9): RS_OWNER_INVITES only (owners.py), no default in code;
  * the site's own ad pixels and Google Ads conversion labels: their defaults have always lived in deploy.yml
    (META_PIXEL_ID, TIKTOK_PIXEL_ID, GADS_SIGNUP_LABEL, GADS_PURCHASE_LABEL), and the app has none, so a test or a
    local run sends no conversions.

    python liveids.py        prints each id, where it comes from (setting / production default / missing)
"""
import os

PRODUCTION = {
    # Stripe: plan prices and products (plans.py), credit pack prices (catalog.py), the Billing Portal (billing.py)
    "STRIPE_PLAN_PRICE_STARTER": "price_1UVh8lRskSkrZOvObNaCGh46",
    "STRIPE_PLAN_PRICE_GROWTH": "price_1UVh8mRskSkrZOvOmLkZYfr5",
    "STRIPE_PLAN_PRICE_SCALE": "price_1UVh8nRskSkrZOvO55rLZ8lD",
    "STRIPE_PLAN_PRODUCT_STARTER": "prod_VWkeMnQ8iKsPWt",
    "STRIPE_PLAN_PRODUCT_GROWTH": "prod_VWkeGs9w4vH9Sq",
    "STRIPE_PLAN_PRODUCT_SCALE": "prod_VWkewxw2bUDvyd",
    "STRIPE_PRICE_STARTER": "price_1UVd2ERskSkrZOvOBwL2BJVU",
    "STRIPE_PRICE_PRO": "price_1UVd2FRskSkrZOvOxLP4KMcR",
    "STRIPE_PRICE_STUDIO": "price_1UVd2GRskSkrZOvOegXnFALF",
    "STRIPE_PORTAL_CONFIG": "bpc_1UVhC6RskSkrZOvOuWBouu50",
    # Google: the site's Google tag (gads.py) and the sign-in OAuth client (google_auth.py; public by design)
    "GADS_TAG_ID": "AW-18543041154",
    "GOOGLE_OAUTH_CLIENT_ID": "107807691054-chboji6914euu71l1g29hkiputd6o97c.apps.googleusercontent.com",
    # Twilio: the secondary customer profile and A2P 10DLC policies (smser.py)
    "TWILIO_SECONDARY_POLICY_SID": "RNdfbf3fae0e1107f8aded0e7cead80bf5",
    "TWILIO_A2P_POLICY_SID": "RNb0d4771c2c98518d916a3d4cd70a8f8b",
    # IndexNow: public key served at /<key>.txt (public_site.py)
    "INDEXNOW_KEY": "93eefb1e35ebe44cf5f9d86a424a9107",
    # The company's postal address in Campaignify's own email footers (messaging.py; a registered agent address)
    "RS_POSTAL_ADDRESS": "30 N Gould St Ste R, Sheridan, WY 82801",
}


def fallback_on() -> bool:
    return (os.environ.get("RS_LIVE_IDS") or "").strip().lower() not in ("settings", "settings-only", "off", "0")


def get(name: str) -> str | None:
    """The setting `name`, else (unless RS_LIVE_IDS=settings) its production value, else None."""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    return PRODUCTION.get(name) if fallback_on() else None


def source(name: str) -> str:
    if (os.environ.get(name) or "").strip():
        return "setting"
    return "production default" if fallback_on() and name in PRODUCTION else "missing"


def report() -> list:
    """[(name, source)] for every live id (no values: an operator page can show it)."""
    return [(k, source(k)) for k in PRODUCTION]


if __name__ == "__main__":
    for k, src in report():
        print(f"{k:<30} {src:<20} {get(k) or ''}")

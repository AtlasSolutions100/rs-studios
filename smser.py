"""Text messages through Twilio (plain HTTPS, no SDK): sending, numbers,
Messaging Services, toll-free verification and A2P 10DLC registration, and
request-signature checks for its webhooks.

Campaignify is the ISV: each workspace gets its own Messaging Service and
number, registered under Campaignify's approved primary customer profile
(TWILIO_PRIMARY_PROFILE_SID). Nothing here runs until TWILIO_ACCOUNT_SID and
TWILIO_AUTH_TOKEN are set (the auth token also validates inbound webhooks).
Tests replace `requests.post` / `requests.get`.
"""
import base64
import hashlib
import hmac
import os

import requests

API = "https://api.twilio.com/2010-04-01"
MSG = "https://messaging.twilio.com/v1"
TRUST = "https://trusthub.twilio.com/v1"
TIMEOUT = 20


class SmsError(RuntimeError):
    pass


def account() -> str:
    return (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()


def token() -> str:
    return (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()


def configured() -> bool:
    return bool(account() and token())


def _auth():
    key, secret = (os.environ.get("TWILIO_API_KEY_SID") or "").strip(), (os.environ.get("TWILIO_API_KEY_SECRET") or "").strip()
    return (key, secret) if key and secret else (account(), token())


def _post(url, data) -> dict:
    if not configured():
        raise SmsError("Text messaging isn't connected.")
    try:
        r = requests.post(url, data=data, auth=_auth(), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SmsError(f"Couldn't reach Twilio: {e}") from e
    if r.status_code >= 400:
        raise SmsError(f"{r.status_code}: {r.text[:240]}")
    return r.json() if r.text else {}


def _get(url, params=None) -> dict:
    if not configured():
        raise SmsError("Text messaging isn't connected.")
    try:
        r = requests.get(url, params=params or {}, auth=_auth(), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SmsError(f"Couldn't reach Twilio: {e}") from e
    if r.status_code >= 400:
        raise SmsError(f"{r.status_code}: {r.text[:240]}")
    return r.json() if r.text else {}


# ---------------------------------------------------------------- sending

def send(messaging_service_sid: str, to: str, body: str, status_callback: str = "") -> str:
    data = {"MessagingServiceSid": messaging_service_sid, "To": to, "Body": body}
    if status_callback:
        data["StatusCallback"] = status_callback
    return _post(f"{API}/Accounts/{account()}/Messages.json", data).get("sid") or ""


def validate_signature(url: str, params: dict, signature: str) -> bool:
    """Twilio's X-Twilio-Signature: base64 HMAC-SHA1 (auth token) of the full URL followed by every POST
    parameter name and value, sorted by name."""
    if not token() or not signature:
        return False
    s = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    want = base64.b64encode(hmac.new(token().encode(), s.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(want, signature)


def sign(url: str, params: dict) -> str:
    """For tests."""
    s = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(token().encode(), s.encode(), hashlib.sha1).digest()).decode()


# ---------------------------------------------------------------- numbers and services

def create_service(name: str, inbound_url: str, status_url: str) -> str:
    d = _post(f"{MSG}/Services", {"FriendlyName": name[:64], "InboundRequestUrl": inbound_url, "InboundMethod": "POST",
                                  "StatusCallback": status_url, "UseInboundWebhookOnNumber": "false",
                                  "UsecaseGroup": "marketing"})
    return d.get("sid") or ""


def find_number(toll_free: bool, area_code: str = "") -> str:
    kind = "TollFree" if toll_free else "Local"
    params = {"SmsEnabled": "true", "PageSize": 1}
    if area_code and not toll_free:
        params["AreaCode"] = area_code
    d = _get(f"{API}/Accounts/{account()}/AvailablePhoneNumbers/US/{kind}.json", params)
    nums = d.get("available_phone_numbers") or []
    if not nums:
        raise SmsError("No numbers available" + (f" in area code {area_code}" if area_code else "") + ". Try another.")
    return nums[0]["phone_number"]


def buy_number(phone: str) -> dict:
    d = _post(f"{API}/Accounts/{account()}/IncomingPhoneNumbers.json", {"PhoneNumber": phone})
    return {"sid": d.get("sid") or "", "phone_number": d.get("phone_number") or phone}


def attach_number(service_sid: str, phone_sid: str):
    _post(f"{MSG}/Services/{service_sid}/PhoneNumbers", {"PhoneNumberSid": phone_sid})


# ---------------------------------------------------------------- toll-free verification

def submit_tollfree(phone_sid: str, f: dict, samples: list[str], optin_url: str, notify_email: str) -> str:
    d = _post(f"{MSG}/Tollfree/Verifications", {
        "TollfreePhoneNumberSid": phone_sid, "BusinessName": f.get("legal_name", ""), "BusinessWebsite": f.get("website", ""),
        "NotificationEmail": notify_email, "UseCaseCategories": "MARKETING", "UseCaseSummary": f.get("use_case", ""),
        "ProductionMessageSample": "\n".join(samples)[:1000], "OptInImageUrls": optin_url, "OptInType": "WEB_FORM",
        "MessageVolume": str(f.get("volume") or "1,000"), "BusinessStreetAddress": f.get("street", ""),
        "BusinessCity": f.get("city", ""), "BusinessStateProvinceRegion": f.get("state", ""),
        "BusinessPostalCode": f.get("zip", ""), "BusinessCountry": "US", "BusinessContactFirstName": f.get("first_name", ""),
        "BusinessContactLastName": f.get("last_name", ""), "BusinessContactEmail": f.get("email", ""),
        "BusinessContactPhone": f.get("phone", ""), "AdditionalInformation": "Opt-in on the business's own web form."})
    return d.get("sid") or ""


def tollfree_status(verification_sid: str) -> str:
    return (_get(f"{MSG}/Tollfree/Verifications/{verification_sid}").get("status") or "").upper()


# ---------------------------------------------------------------- A2P 10DLC (ISV: secondary profile -> brand -> campaign)

def create_customer_profile(f: dict, notify_email: str) -> str:
    """A secondary customer profile under Campaignify's primary one, with the business's details, then submitted."""
    policy = os.environ.get("TWILIO_SECONDARY_POLICY_SID", "RNdfbf3fae0e1107f8aded0e7cead80bf5")
    prof = _post(f"{TRUST}/CustomerProfiles", {"FriendlyName": f.get("legal_name", "")[:60], "Email": notify_email,
                                              "PolicySid": policy}).get("sid", "")
    biz = _post(f"{TRUST}/EndUsers", {"FriendlyName": "business", "Type": "customer_profile_business_information",
                                      "Attributes": _json({"business_name": f.get("legal_name"), "business_type": f.get("business_type", "LLC"),
                                                           "business_registration_identifier": "EIN",
                                                           "business_registration_number": f.get("ein", ""),
                                                           "business_identity": "direct_customer", "website_url": f.get("website"),
                                                           "business_industry": f.get("industry", "RETAIL")})}).get("sid", "")
    rep = _post(f"{TRUST}/EndUsers", {"FriendlyName": "rep", "Type": "authorized_representative_1",
                                      "Attributes": _json({"first_name": f.get("first_name"), "last_name": f.get("last_name"),
                                                           "email": f.get("email"), "phone_number": f.get("phone"),
                                                           "job_position": f.get("job_title", "Owner"), "business_title": "Owner"})}).get("sid", "")
    addr = _post(f"{API}/Accounts/{account()}/Addresses.json", {"CustomerName": f.get("legal_name"), "Street": f.get("street"),
                                                               "City": f.get("city"), "Region": f.get("state"),
                                                               "PostalCode": f.get("zip"), "IsoCountry": "US"}).get("sid", "")
    doc = _post(f"{TRUST}/SupportingDocuments", {"FriendlyName": "address", "Type": "customer_profile_address",
                                                 "Attributes": _json({"address_sids": addr})}).get("sid", "")
    primary = os.environ.get("TWILIO_PRIMARY_PROFILE_SID", "")
    for sid in (biz, rep, doc, primary):
        if sid:
            _post(f"{TRUST}/CustomerProfiles/{prof}/EntityAssignments", {"ObjectSid": sid})
    _post(f"{TRUST}/CustomerProfiles/{prof}", {"Status": "pending-review"})
    return prof


def profile_status(profile_sid: str) -> str:
    return (_get(f"{TRUST}/CustomerProfiles/{profile_sid}").get("status") or "").lower()


def create_brand(profile_sid: str, low_volume: bool) -> str:
    """A2P trust product for the brand, then the brand registration."""
    tp = _post(f"{TRUST}/TrustProducts", {"FriendlyName": "a2p", "Email": os.environ.get("TWILIO_NOTIFY_EMAIL", ""),
                                          "PolicySid": os.environ.get("TWILIO_A2P_POLICY_SID", "RNb0d4771c2c98518d916a3d4cd70a8f8b")}
               ).get("sid", "")
    _post(f"{TRUST}/TrustProducts/{tp}/EntityAssignments", {"ObjectSid": profile_sid})
    _post(f"{TRUST}/TrustProducts/{tp}", {"Status": "pending-review"})
    d = _post(f"{MSG}/a2p/BrandRegistrations", {"CustomerProfileBundleSid": profile_sid, "A2PProfileBundleSid": tp,
                                                "SkipAutomaticSecVet": "true" if low_volume else "false"})
    return d.get("sid") or ""


def brand_status(brand_sid: str) -> str:
    return (_get(f"{MSG}/a2p/BrandRegistrations/{brand_sid}").get("status") or "").upper()


def create_campaign(service_sid: str, brand_sid: str, f: dict, samples: list[str]) -> str:
    """The marketing campaign (use case + up to five sample messages, repeated MessageSamples fields)."""
    d = _post(f"{MSG}/Services/{service_sid}/Compliance/Usa2p", {
        "BrandRegistrationSid": brand_sid, "Description": f.get("use_case", "")[:4096],
        "MessageFlow": f.get("message_flow", "")[:2048], "UsAppToPersonUsecase": "MARKETING",
        "HasEmbeddedLinks": "true", "HasEmbeddedPhone": "false", "MessageSamples": [x[:1024] for x in samples[:5]]})
    return d.get("sid") or ""


def campaign_status(service_sid: str, campaign_sid: str) -> str:
    return (_get(f"{MSG}/Services/{service_sid}/Compliance/Usa2p/{campaign_sid}").get("campaign_status") or "").upper()


def _json(d: dict) -> str:
    import json
    return json.dumps({k: v for k, v in d.items() if v not in (None, "")})

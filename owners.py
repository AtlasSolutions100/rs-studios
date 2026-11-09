"""Owner accounts: one-time setup links for the owners (Koby and Travis).

A code works once, only with its own email, and makes that person an admin of
the "Rector & Stull" workspace. The list is the setting RS_OWNER_INVITES
("email:sha256-of-code,..."; only the SHA-256 of each code, never the code).
No owner email or code hash lives in the source (SEC-9, roadmap M7.4): unset,
there are no owner setup links.
"""
import hashlib
import os

def table() -> dict:
    env = os.environ.get("RS_OWNER_INVITES") or ""
    out = {}
    for part in env.split(","):
        if ":" in part:
            email, h = part.strip().rsplit(":", 1)
            out[h.strip()] = email.strip().lower()
    return out


def owner_email(code: str) -> str | None:
    """The email this owner code is for, or None."""
    if not code:
        return None
    return table().get(hashlib.sha256(code.strip().encode()).hexdigest())

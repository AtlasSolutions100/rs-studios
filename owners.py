"""Owner accounts: one-time setup links for the Rector & Stull admins.

Only the SHA-256 of each code lives here; the codes themselves were handed to
Koby. A code works once, only with its own email, and makes that person an
admin of the "Rector & Stull" workspace. RS_OWNER_INVITES ("email:sha256,...")
replaces the list (the tests use it).
"""
import hashlib
import os

DEFAULT = {
    "ef32ab7259dbb618cf9736b1bc3c65eee4fba6e58b6a68640b67be59f53fdf30": "koby@rectorandstull.com",
    "7eab982314d92a8341cf5da0116d408a801e496b24e11d1c5963bb552a10eb95": "travis@rectorandstull.com",
}


def table() -> dict:
    env = os.environ.get("RS_OWNER_INVITES")
    if env is None:
        return DEFAULT
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

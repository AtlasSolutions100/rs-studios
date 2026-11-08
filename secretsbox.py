"""The token box: AES-GCM encryption for secrets we keep in the database (roadmap M2.1).

Ad-platform tokens (ad_connections.token_enc, refresh_token_enc, pixels.capi_token_enc) are
stored only as boxes made here. The key never touches the database or the logs:

    TOKEN_KEY        32 random bytes, base64. Make one with:
                     python3 -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
    TOKEN_KEYS_OLD   optional: earlier TOKEN_KEY values, comma separated, still accepted for
                     reading while `python3 secretsbox.py rotate` re-seals every box with TOKEN_KEY.
                     "none" (or unset) means no old key. deploy.yml forwards it (L-15); the rotate
                     steps are written there.

A box is text: "cfy1:<key id>:<base64url(nonce + ciphertext + tag)>". The key id is the first
8 hex digits of sha256(key), so a box says which key opens it without saying anything about
the key. Each box is sealed to a context string (the AAD, e.g. "ad_connections:12"), so a box
copied into another workspace's row does not open there.

Without TOKEN_KEY nothing is sealed or opened (NotConfigured): callers refuse to connect an
account rather than store a token in the clear.
"""
import base64
import binascii
import hashlib
import os

PREFIX = "cfy1"
NONCE = 12


class NotConfigured(RuntimeError):
    """TOKEN_KEY is not set (or is not 32 bytes of base64)."""


class BoxError(ValueError):
    """A box that doesn't open: wrong key, wrong context, or damaged. Says nothing more."""


def _decode_key(raw: str) -> bytes:
    raw = (raw or "").strip()
    if not raw:
        raise NotConfigured("TOKEN_KEY is not set")
    try:
        key = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_" if ("-" in raw or "_" in raw) else None,
                               validate=True)
    except (binascii.Error, ValueError):
        raise NotConfigured("TOKEN_KEY is not base64") from None
    if len(key) != 32:
        raise NotConfigured("TOKEN_KEY must be 32 bytes")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def _keys():
    """(current key, {key id: key} for every key we may open with)."""
    current = _decode_key(os.environ.get("TOKEN_KEY", ""))
    keys = {key_id(current): current}
    for raw in (os.environ.get("TOKEN_KEYS_OLD") or "").split(","):
        if raw.strip() and raw.strip().lower() not in ("none", "off", "0"):   # "none": rotation finished
            k = _decode_key(raw)
            keys.setdefault(key_id(k), k)
    return current, keys


def configured() -> bool:
    try:
        _keys()
        return True
    except NotConfigured:
        return False


def seal(plaintext: str, aad: str = "") -> str:
    """The box for `plaintext` under TOKEN_KEY, bound to `aad`."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if plaintext is None:
        raise ValueError("nothing to seal")
    current, _ = _keys()
    nonce = os.urandom(NONCE)
    ct = AESGCM(current).encrypt(nonce, str(plaintext).encode("utf-8"), (aad or "").encode("utf-8"))
    return f"{PREFIX}:{key_id(current)}:" + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")


def open_box(box, aad: str = "") -> str:
    """The plaintext in `box`, or BoxError. Never returns or logs anything about the key."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if isinstance(box, (bytes, bytearray, memoryview)):
        box = bytes(box).decode("ascii", "replace")
    parts = str(box or "").split(":")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise BoxError("not a token box")
    _, keys = _keys()
    key = keys.get(parts[1])
    if key is None:
        raise BoxError("sealed with a key we don't have")
    try:
        raw = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        if len(raw) <= NONCE + 16:
            raise BoxError("box too short")
        return AESGCM(key).decrypt(raw[:NONCE], raw[NONCE:], (aad or "").encode("utf-8")).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError):
        raise BoxError("box does not open") from None


def needs_rotation(box) -> bool:
    if isinstance(box, (bytes, bytearray, memoryview)):
        box = bytes(box).decode("ascii", "replace")
    parts = str(box or "").split(":")
    current, _ = _keys()
    return len(parts) == 3 and parts[1] != key_id(current)


def rotate(box, aad: str = "") -> str:
    """The same secret sealed under the current TOKEN_KEY (unchanged when it already is)."""
    if not needs_rotation(box):
        return box if isinstance(box, str) else bytes(box).decode("ascii")
    return seal(open_box(box, aad), aad)


# Every boxed column: (table, column). The AAD of a row is "<table>:<workspace_id>".
BOXED = [("ad_connections", "token_enc"), ("ad_connections", "refresh_token_enc"), ("pixels", "capi_token_enc")]


def aad_for(table: str, workspace_id) -> str:
    return f"{table}:{int(workspace_id)}"


def rotate_db(conn) -> int:
    """Re-seal every box under TOKEN_KEY (after moving the old key to TOKEN_KEYS_OLD). Returns how many changed."""
    n = 0
    for table, col in BOXED:
        for r in conn.execute(f"SELECT id, workspace_id, {col} AS box FROM {table} WHERE {col} IS NOT NULL").fetchall():
            if needs_rotation(r["box"]):
                conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (rotate(r["box"], aad_for(table, r["workspace_id"])),
                                                                       r["id"]))
                n += 1
    conn.commit()
    return n


if __name__ == "__main__":   # python3 secretsbox.py rotate   (inside the app container)
    import sys
    if sys.argv[1:] == ["rotate"]:
        import db
        with db.connect() as c:
            print(f"re-sealed {rotate_db(c)} token box(es) under key {key_id(_keys()[0])}")
    else:
        print(__doc__)

"""Admin credential hashing and signed session tokens (stdlib only).

The dashboard is the only thing that needs protecting, so we keep this small:
passwords are stored as a PBKDF2-HMAC-SHA256 hash + random salt (never plaintext),
and login state rides in a signed cookie verified with HMAC-SHA256. The signing
secret is derived from the stored credential, so resetting the password also
invalidates every existing session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

# PBKDF2 work factor. Cheap enough for a Pi to verify on each login, dear enough
# to make offline guessing of a leaked hash painful.
_ITERATIONS = 200_000
_SALT_BYTES = 16


@dataclass(frozen=True)
class PasswordHash:
    """A stored password verifier: the salt and derived hash, both hex."""

    salt: str
    hash: str
    iterations: int = _ITERATIONS


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str) -> PasswordHash:
    """Hash a plaintext password with a fresh random salt."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _ITERATIONS)
    return PasswordHash(salt=salt.hex(), hash=digest.hex(), iterations=_ITERATIONS)


def verify_password(password: str, stored: PasswordHash) -> bool:
    """Check a plaintext password against a stored hash in constant time."""
    try:
        salt = bytes.fromhex(stored.salt)
    except ValueError:
        return False
    digest = _derive(password, salt, stored.iterations)
    return hmac.compare_digest(digest.hex(), stored.hash)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_session(username: str, secret: str) -> str:
    """Mint a signed session token of the form ``<username>.<signature>``."""
    payload = _b64(username.encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"


def verify_session(token: str, secret: str) -> str | None:
    """Return the username from a valid signed token, or None if it doesn't verify."""
    if not token or "." not in token:
        return None
    payload, _, sig = token.partition(".")
    expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        return _unb64(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None

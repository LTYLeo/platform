"""Password hashing, session tokens and API keys.

Password hashing uses :mod:`hashlib.scrypt` from the standard library, so the
service has **no compiled dependencies** and installs anywhere Python runs.
scrypt is memory-hard and is one of the KDFs OWASP accepts for password storage.

The stored hash is a versioned, self-describing string::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

Keeping the parameters inside the value means they can be raised later without
invalidating existing passwords -- ``needs_rehash()`` reports when an old hash
should be upgraded on the next successful login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Cost parameters. n=2**14 with r=8 needs ~16 MiB and ~20 ms on a laptop, which
# is a reasonable interactive-login target. Raise n as hardware improves.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

SESSION_TOKEN_BYTES = 32
API_KEY_BYTES = 24
API_KEY_PREFIX = "sk-tai-"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN
    )


def hash_password(password: str) -> str:
    """Hash a plaintext password into the versioned storage format."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a password against a stored hash."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = _scrypt(password, _unb64(salt_b64), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, _unb64(hash_b64))


def needs_rehash(stored: str) -> bool:
    """True when the stored hash uses weaker parameters than we now use."""
    try:
        scheme, n, r, p, _salt, _hash = stored.split("$")
    except ValueError:
        return True
    if scheme != "scrypt":
        return True
    return (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


# --------------------------------------------------------------------------- #
# Session tokens
# --------------------------------------------------------------------------- #

def new_session_token() -> str:
    """A high-entropy opaque token. This value is only ever sent to the client."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """What we actually store.

    The database therefore never holds anything that could be replayed as a
    valid session cookie if it leaked.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #

def new_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, fingerprint)`` for a freshly minted key.

    The plaintext is shown to the user exactly once; only the fingerprint and a
    short display prefix are persisted.
    """
    secret = secrets.token_hex(API_KEY_BYTES)
    plaintext = f"{API_KEY_PREFIX}{secret}"
    return plaintext, plaintext[:11], token_fingerprint(plaintext)


def api_key_fingerprint(plaintext: str) -> str:
    return token_fingerprint(plaintext)

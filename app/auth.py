"""Password hashing and opaque bearer-session helpers for the local API."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from app import database


PASSWORD_ITERATIONS = 600_000
SESSION_HOURS = 8
_SALT_BYTES = 16
_HASH_BYTES = 32


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128 or not password.strip():
        raise ValueError("Passwords must contain 12–128 characters.")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS, dklen=_HASH_BYTES
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Verify a stored PBKDF2 hash; malformed hashes fail closed."""
    try:
        algorithm, rounds_text, salt_hex, digest_hex = (stored_hash or "").split("$", 3)
        rounds = int(rounds_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if (
            algorithm != "pbkdf2_sha256"
            or rounds < PASSWORD_ITERATIONS
            or rounds > 2_000_000
            or len(salt) != _SALT_BYTES
            or len(expected) != _HASH_BYTES
        ):
            return False
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, rounds, dklen=_HASH_BYTES
    )
    return hmac.compare_digest(actual, expected)


def burn_password_check(password: str) -> None:
    """Reduce username-enumeration timing differences for unknown users."""
    hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes(_SALT_BYTES), PASSWORD_ITERATIONS, dklen=_HASH_BYTES
    )


def issue_session(user_id: str) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=SESSION_HOURS)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    database.create_auth_session(token_hash, user_id, now.isoformat(), expires.isoformat())
    return token, expires


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate_token(token: str) -> dict | None:
    return database.get_session_user(token_digest(token), datetime.now(timezone.utc).isoformat())


def revoke_token(token: str) -> bool:
    return database.revoke_auth_session(
        token_digest(token), datetime.now(timezone.utc).isoformat()
    )

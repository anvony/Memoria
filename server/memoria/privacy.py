"""The Private space: a password-gated corner of the library.

Design notes:
- The password is never stored — only a salted scrypt hash in the kv table.
  scrypt is deliberately slow/memory-hard, so even if memoria.db leaks,
  brute-forcing the password is expensive.
- Unlocking returns a random token that lives only in this process's memory.
  Closing the app locks the space again. JSON endpoints take the token in the
  X-Privacy-Token header; media endpoints take it as a ?pt= query parameter
  because <img> tags can't send headers.
- This protects against shoulder-surfing and casual snooping, which is the
  right threat model for a local single-user app. It is NOT encryption: the
  original files on disk are untouched (that's the whole point of Memoria).
"""

from __future__ import annotations

import hashlib
import secrets

from . import db

_tokens: set[str] = set()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


def configured() -> bool:
    return db.kv_get("privacy_hash") is not None


def set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    db.kv_set("privacy_hash", f"{salt.hex()}:{_hash(password, salt).hex()}")


def verify(password: str) -> bool:
    stored = db.kv_get("privacy_hash")
    if not stored:
        return False
    salt_hex, hash_hex = stored.split(":", 1)
    candidate = _hash(password, bytes.fromhex(salt_hex)).hex()
    return secrets.compare_digest(candidate, hash_hex)


def unlock(password: str) -> str | None:
    """Returns a session token, or None if the password is wrong."""
    if not verify(password):
        return None
    token = secrets.token_hex(16)
    _tokens.add(token)
    return token


def valid(token: str | None) -> bool:
    return token is not None and token in _tokens


def lock(token: str | None) -> None:
    if token:
        _tokens.discard(token)

"""Argon2id password hashing wrapper.

One module-level :class:`PasswordHasher` with argon2-cffi's default
parameters (argon2id, m=64 MiB, t=3, p=4) — the RFC 9106 "second
recommended configuration" as of 2023. Good enough for interactive
logins on the GUI master; re-evaluate for batch / high-QPS auth.

Why a wrapper module instead of raw argon2-cffi calls:

* single place to tune parameters if/when we raise the cost,
* single place to call :meth:`check_needs_rehash` on every successful
  login so users roll forward transparently,
* keeps backends (local_db, tests) from importing argon2 directly.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Return a PHC-encoded argon2id hash for ``plaintext``."""
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """Constant-time check. Returns False on mismatch OR corrupt hash."""
    try:
        _hasher.verify(stored_hash, plaintext)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    """True if ``stored_hash`` was produced with weaker parameters."""
    return _hasher.check_needs_rehash(stored_hash)

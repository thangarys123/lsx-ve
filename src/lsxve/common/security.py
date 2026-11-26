"""Password hashing and token primitives (stdlib only).

Passwords use scrypt (N=2^15, r=8, p=1) with a per-password 16-byte salt.
Format: scrypt$<N>$<r>$<p>$<salt b64>$<hash b64>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_N, _R, _P, _LEN = 2 ** 15, 8, 1, 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_LEN, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def _verify_crypt(password: str, encoded: str) -> bool:
    """SHA-256/512 crypt hashes ($5$..., $6$...), e.g. the root password imported by the GUI installer."""
    try:
        import crypt  # Python <= 3.12 (Rocky Linux 9 ships 3.9)
    except ImportError:
        return False
    out = crypt.crypt(password, encoded) or ""
    return hmac.compare_digest(out.encode(), encoded.encode())


def needs_rehash(encoded: str) -> bool:
    return not encoded.startswith(f"scrypt${_N}$")


def verify_password(password: str, encoded: str) -> bool:
    if encoded.startswith(("$6$", "$5$")):
        return _verify_crypt(password, encoded)
    try:
        algo, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                            dklen=len(expected), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# A constant hash used to keep timing similar for unknown users.
_DUMMY = None


def dummy_verify(password: str) -> None:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("lsxve-dummy-password")
    verify_password(password, _DUMMY)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


def consteq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

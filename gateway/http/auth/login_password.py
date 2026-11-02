# meiva:note
# The web login password as a stored record: derivation, parsing and verification.
#
# The record is the only form the password is kept in: a JSON object naming the
# KDF (`scrypt`), its cost parameters, a random salt and the derived hash. Every
# function here is a pure function of its arguments (the salt comes from the
# caller), so the gateway's file and entropy handling stays in
# `gateway.http.auth.login_password_store`.

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveInt, ValidationError

# meiva:note
# The login page is reachable by an online guesser, and `gateway.login_max_failures` bounds the
# failures per client address, not per credential: the length floor is what keeps a short word
# out of the set a patient guesser can cover. Twelve characters admits a passphrase and refuses
# the words and dates people reach for first.
MIN_PASSWORD_LENGTH = 12

SALT_BYTES = 16

_ALGORITHM = "scrypt"
# meiva:note
# The cost of one derivation (N=2**15, r=8, p=1: about 32 MiB of memory and some tens of
# milliseconds), paid on every password login and every `PUT /api/auth/password`.
_COST_N = 2**15
_COST_R = 8
_COST_P = 1
_HASH_BYTES = 32
# meiva:note
# scrypt needs 128 * r * (N + p + 1) bytes (33.6 MB at the cost above); a stored record that asks
# for more than this is refused instead of allocating what a damaged file names.
_MAX_MEMORY_BYTES = 64 * 1024 * 1024


# meiva:note
# The stored record is not a well-formed web login password record.
class PasswordRecordError(ValueError): ...


@dataclass(frozen=True, slots=True)
class PasswordRecord:
    n: int
    r: int
    p: int
    salt: bytes
    hash: bytes


# meiva:note
# Why `password` cannot be set as the login password, or None when it can.
def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"the password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    try:
        return hashlib.scrypt(
            # meiva:note
            # A lone surrogate cannot arrive in UTF-8 text but can in a JSON string; keep it a
            # distinct, rejectable password instead of a UnicodeEncodeError.
            password.encode("utf-8", errors="surrogatepass"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_MAX_MEMORY_BYTES,
            dklen=_HASH_BYTES,
        )
    except ValueError as exc:
        raise PasswordRecordError(f"scrypt refuses the record's parameters: {exc}") from exc


# meiva:note
# The record bytes that store `password` under `salt` (exactly `SALT_BYTES` of entropy).
#
# Raises:
#     ValueError: `password` fails `password_problem`, or `salt` has the wrong length.
def render_record(password: str, salt: bytes) -> bytes:
    problem = password_problem(password)
    if problem is not None:
        raise ValueError(problem)
    if len(salt) != SALT_BYTES:
        raise ValueError(f"the salt must be {SALT_BYTES} bytes, got {len(salt)}")
    derived = _derive(password, salt, n=_COST_N, r=_COST_R, p=_COST_P)
    record = {
        "algorithm": _ALGORITHM,
        "n": _COST_N,
        "r": _COST_R,
        "p": _COST_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(derived).decode("ascii"),
    }
    return (json.dumps(record, sort_keys=True) + "\n").encode("ascii")


# meiva:note
# The JSON object `render_record` writes; nothing else is a record.
class _StoredRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    algorithm: Literal["scrypt"]
    n: PositiveInt
    r: PositiveInt
    p: PositiveInt
    salt: str
    hash: str


def _decoded(value: str, name: str, length: int) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PasswordRecordError(f"{name!r} is not valid base64") from exc
    if len(decoded) != length:
        raise PasswordRecordError(f"{name!r} must decode to {length} bytes, got {len(decoded)}")
    return decoded


# meiva:note
# The record `raw` holds.
#
# Raises:
#     PasswordRecordError: `raw` is not exactly the object `render_record` writes: not JSON,
#         another algorithm, a missing or extra field, an unusable cost or a wrong-length
#         salt or hash. A damaged record is never read as "no password set".
def parse_record(raw: bytes) -> PasswordRecord:
    try:
        stored = _StoredRecord.model_validate_json(raw)
    except ValidationError as exc:
        # meiva:note
        # Without `input`: a record's salt and hash stay out of the error text and the logs.
        problems = exc.errors(include_input=False, include_url=False, include_context=False)
        raise PasswordRecordError(
            "; ".join(f"{'.'.join(map(str, p['loc'])) or 'record'}: {p['msg']}" for p in problems)
        ) from exc
    if stored.n < 2 or stored.n & (stored.n - 1):
        raise PasswordRecordError("'n' must be a power of two of at least 2")
    if 128 * stored.r * (stored.n + stored.p + 1) > _MAX_MEMORY_BYTES:
        raise PasswordRecordError("the cost parameters need more memory than a record may ask for")
    return PasswordRecord(
        n=stored.n,
        r=stored.r,
        p=stored.p,
        salt=_decoded(stored.salt, "salt", SALT_BYTES),
        hash=_decoded(stored.hash, "hash", _HASH_BYTES),
    )


# meiva:note
# Whether `password` is the one `record` stores, compared in constant time.
def verify_password(record: PasswordRecord, password: str) -> bool:
    derived = _derive(password, record.salt, n=record.n, r=record.r, p=record.p)
    return hmac.compare_digest(derived, record.hash)


# meiva:note
# SHA-256 hex of the record bytes: the credential digest a password session binds to.
#
# A password set again draws a new salt and so writes different bytes, which is what ends
# every session the old record minted even when the new password equals the old one.
def record_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

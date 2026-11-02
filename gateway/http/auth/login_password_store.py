# meiva:note
# The web login password record on disk: `$MEIVA_HOME/web-login-password.json`.
#
# The file is the password's only stored form (a salted scrypt hash, see
# `gateway.http.auth.login_password`), owner-only, written atomically. The cluster secret never
# appears in it and it never appears in `.env`, where agents and schedule scripts can read.

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from gateway.http.auth.login_password import (
    SALT_BYTES,
    PasswordRecord,
    PasswordRecordError,
    parse_record,
    password_problem,
    record_digest,
    render_record,
)

# meiva:note
# The file name, not a credential.
LOGIN_PASSWORD_FILE = "web-login-password.json"  # noqa: S105
_MAX_RECORD_BYTES = 4096


def login_password_path(home: Path) -> Path:
    return home / LOGIN_PASSWORD_FILE


# meiva:note
# The record a file held and the digest of its bytes (`record_digest`).
@dataclass(frozen=True, slots=True)
class StoredPassword:
    record: PasswordRecord
    digest: str


# meiva:note
# This gateway's web login password record, read once per file revision.
#
# Owned by the gateway app (`app.state.login_passwords`). The cache is keyed by the file's
# identity (inode, mtime, size), so a replaced or rewritten file is re-read and re-validated.
class LoginPasswords:
    # meiva:note
    # `rng` is where a new record's salt comes from; the root hands in a cryptographically
    # secure generator.
    def __init__(self, rng: random.Random) -> None:
        self._cache: dict[Path, tuple[tuple[int, int, int], StoredPassword]] = {}
        self._rng = rng

    # meiva:note
    # The stored password, or None while none has been set.
    #
    # Raises:
    #     PasswordRecordError: the file exists but is not a well-formed record. A damaged
    #         record is never read as "no password set": that would silently end the
    #         password's sessions and refuse its login without saying why.
    #     RuntimeError: the file is a symlink, not a regular file, or not owner-only.
    def current(self, home: Path) -> StoredPassword | None:
        from base.host.private_storage import private_file_problem
        from base.host.verified_file.reader import regular_bytes

        path = login_password_path(home)
        try:
            info = path.lstat()
        except FileNotFoundError:
            self._cache.pop(home, None)
            return None
        identity = (info.st_ino, info.st_mtime_ns, info.st_size)
        cached = self._cache.get(home)
        if cached is not None and cached[0] == identity:
            return cached[1]
        problem = private_file_problem(path)
        if problem is None and info.st_mode & 0o077:
            problem = "is not owner-only"
        if problem is not None:
            raise RuntimeError(f"{path}: {problem}")
        raw = regular_bytes(path, max_bytes=_MAX_RECORD_BYTES)
        try:
            record = parse_record(raw)
        except PasswordRecordError as exc:
            raise PasswordRecordError(f"{path} is not a web login password record: {exc}") from exc
        stored = StoredPassword(record=record, digest=record_digest(raw))
        self._cache[home] = (identity, stored)
        return stored

    # meiva:note
    # Store `password` under a fresh salt, replacing any earlier record.
    #
    # A fresh salt makes the bytes, and so the digest every password session is bound to,
    # different from the record it replaces even for the same password: every session the
    # earlier record minted ends.
    #
    # Raises:
    #     ValueError: `password` fails `password_problem`.
    def set(self, home: Path, password: str) -> None:
        from base.host.private_storage import write_private_bytes

        problem = password_problem(password)
        if problem is not None:
            raise ValueError(problem)
        raw = render_record(password, self._rng.randbytes(SALT_BYTES))
        write_private_bytes(login_password_path(home), raw)

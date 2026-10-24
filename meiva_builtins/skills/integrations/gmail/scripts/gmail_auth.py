# meiva:note
# Which credentials the gmail skill logs in with, as values. Where they may come from
# (the GMAIL_ACCOUNT override, the secrets file, the macOS Keychain entry), how each source's
# raw text becomes a login or an app password, and what each failure says. Reading the file,
# running `security` and the login itself live in imap.py.

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

KEYCHAIN_SERVICE = "meiva-gmail-imap"

KEYCHAIN_TIMEOUT_S = 15.0

_RE_KEYCHAIN_ACCOUNT = re.compile(r'"acct"<blob>="([^"]+)"')

_CREATE_ENTRY = f"security add-generic-password -a <you@gmail.com> -s {KEYCHAIN_SERVICE} -w"


# meiva:note
# The mailbox identity and secret come from machine-level sources, never from a cluster
# home: `account_env` is the stripped GMAIL_ACCOUNT override (empty when unset),
# `password_file` the plain-text app-password file read before the Keychain, `security_cmd`
# the Keychain command-line program.
@dataclass(frozen=True)
class AuthSources:
    account_env: str
    password_file: Path
    security_cmd: str
    keychain_timeout_s: float

    def keychain_argv(self, *extra: str) -> list[str]:
        return [self.security_cmd, "find-generic-password", "-s", KEYCHAIN_SERVICE, *extra]


# meiva:note
# A Gmail IMAP operation failed: Keychain unreadable, login rejected, a search Gmail refused,
# or a response whose shape we did not expect.
class GmailError(Exception):
    pass


KeychainFailure = Literal["program_missing", "timed_out", "refused"]


# meiva:note
# The error for a failed Keychain read: the `security` program is absent (not macOS), it did
# not answer within `timeout_s` (keychain locked / prompt pending), or it exited non-zero
# with `stderr`.
def keychain_error(kind: KeychainFailure, stderr: str, timeout_s: float) -> GmailError:
    prefix = f"could not read Keychain entry (service={KEYCHAIN_SERVICE!r}): "
    match kind:
        case "program_missing":
            return GmailError(
                f"{prefix}security command not found (not macOS). Create it with: {_CREATE_ENTRY}"
            )
        case "timed_out":
            return GmailError(
                f"{prefix}security command timed out after {timeout_s:g}s "
                f"(keychain locked / prompt pending)"
            )
        case "refused":
            return GmailError(
                f"{prefix}{stderr.strip() or 'not found'}. Create it with: {_CREATE_ENTRY}"
            )
        case _:
            assert_never(kind)


# meiva:note
# The `acct` label of the Keychain entry's attribute dump: the login to use.
def account_from_keychain_dump(dump: str) -> str:
    m = _RE_KEYCHAIN_ACCOUNT.search(dump)
    if not m:
        raise GmailError(f"Keychain entry {KEYCHAIN_SERVICE!r} has no account (acct) attribute")
    return m.group(1)


# meiva:note
# Gmail accepts the 16-char app password with or without its display spaces; strip them so a
# copy-paste that kept the "abcd efgh ..." grouping still works. Empty means no password.
def normalize_password(raw: str) -> str:
    return raw.strip().replace(" ", "")


def no_password_error(password_file: Path) -> GmailError:
    return GmailError(
        f"no Gmail app password: file {password_file} unreadable/missing and "
        f"Keychain entry (service={KEYCHAIN_SERVICE!r}) unreadable. Create the file (chmod 600) or: "
        f"{_CREATE_ENTRY}"
    )

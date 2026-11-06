# meiva:note
# What a log record must not carry: a password inside a URL, a value named like a secret. One
# process-wide loguru patcher, installed at the seams that open a process's sinks. It runs before
# every handler, so the human stream, the per-process file sink and the event pipeline all see the
# same masked record -- and on the way to disk only: what the caller saw (an exec output returned to
# an agent, a value an operator passed in) is unchanged.
#
# Three rules, and no guessing beyond them: a URL's userinfo password; the value of a mapping key
# named exactly `token`, `secret` or `password` (case-insensitive, also when that mapping was
# serialized into the message); and the value of an environment assignment whose key is upper-case
# and ends in `_TOKEN`/`_PASSWORD`/`_SECRET` (`MEIVA_API_TOKEN=...`). A key that merely contains one
# of those words stays visible, and so does everything else, because the evidence a later reader
# needs is usually right next to the credential. The asymmetry decides the edges: masking one more
# non-secret costs a little readability, missing one real password costs an irreversible leak.
# Task #5993: an exec output echoing a data-plane URL was written verbatim to agent_host.log, the
# events JSONL and the unit's output.log.
#
# Known gaps, recorded here rather than in a pull request body, because the person who changes this
# file is the one who needs them:
#   * A secret passed as a named argument survives in `extra`; only the message text is masked.
#     `logger.info("{v}", v=secret)` loses the value from the sentence and keeps it in the record.
#     Masking by the argument's name would mean guessing, so the rule stays on the text.
#   * A value truncated mid-string (a log line cut to its length limit) has no closing quote for
#     the mapping rule to match, and half a credential is not usable. Closing it would mean
#     swallowing the rest of the line, which costs more than it saves.

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TypeGuard

import loguru

__all__ = ["REDACTED", "install_log_redaction", "redact_record", "redact_text"]

REDACTED = "***"

_SECRET_KEYS = frozenset({"token", "secret", "password"})

# meiva:note
# A password inside a URL's userinfo, matched on raw text rather than through `urlsplit` so a value
# the parser would refuse (an outer-quoted .env line, a host it cannot read) still loses its
# password before any sink sees it. The user name stays: it names the identity the URL dials as,
# which is what makes a redacted line still diagnosable. This is the same rule as
# `base.host.net.url_text.redacted_url`, written here because `base.log` may not import `base.host`
# (the root manifest's `"log" = ["paths"]`, and `host` declares `log`).
_USERINFO_PASSWORD = re.compile(r"(://[^/\s@]*:)[^/\s@]*@")

# meiva:note
# The second rule applied to a mapping that reached the log as text: a JSON object serialized into a
# message or an exec output. The key list is exact, so `token_count`, `tokenizer` and
# `token_expires_at` keep their values.
_SECRET_IN_TEXT = re.compile(
    r'("(?:token|secret|password)"\s*:\s*)"(?:[^"\\]|\\.)*"', re.IGNORECASE
)

# meiva:note
# A value a process echoed as an environment assignment: `MEIVA_API_TOKEN=...`,
# `DB_PASSWORD="..."`, `API_SECRET: ...`. The key shape is fixed -- upper-case, digits and
# underscores, ending in `_TOKEN`/`_PASSWORD`/`_SECRET` -- so an ordinary line such as
# `token_count=3` or `TOKENIZER_PATH=/x` keeps its value, and the key itself stays visible because
# a reader has to know which variable leaked. The value runs to whitespace; quotes around it are
# kept.
_ENV_SECRET = re.compile(
    r'\b([A-Z][A-Z0-9_]*(?:_TOKEN|_PASSWORD|_SECRET)\b\s*[:=]\s*)(["\']?)([^\s"\']+)\2'
)


# meiva:note
# `text` with URL passwords and secret-named JSON values replaced by `***`. A string that can carry
# neither comes back unchanged and uncopied, so the common record costs one substring search per
# rule.
def redact_text(text: str) -> str:
    if "://" in text:
        text = _USERINFO_PASSWORD.sub(r"\1" + REDACTED + "@", text)
    if '"' in text:
        text = _SECRET_IN_TEXT.sub(lambda match: match.group(1) + '"' + REDACTED + '"', text)
    if "_TOKEN" in text or "_PASSWORD" in text or "_SECRET" in text:
        text = _ENV_SECRET.sub(
            lambda match: match.group(1) + match.group(2) + REDACTED + match.group(2), text
        )
    return text


# meiva:note
# A mapping is rebuilt rather than mutated: the caller that passed it into the log call keeps its
# own object, and the record gets the masked copy. The key rule lives here and not only in the
# string rule, so a value handed in as a mapping is masked by its name without being serialized
# first.
def _redact_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    return {
        key: REDACTED
        if isinstance(key, str) and key.lower() in _SECRET_KEYS
        else _redact_value(item)
        for key, item in mapping.items()
    }


# meiva:note
# The narrowing `base.host.boundary.json_values` owns for decoded JSON, in the one place `base.log`
# can have it: the root manifest declares `"log" = ["paths"]` and `base.host` declares `base.log`,
# so importing the shared pair would close a cycle. A key that is not a string cannot be a secret
# name, so it keeps its value untouched.
def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_named_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return _is_mapping(value) and all(isinstance(key, str) for key in value)


def _is_sequence(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if _is_named_mapping(value):
        return _redact_mapping(value)
    if _is_sequence(value):
        return [_redact_value(item) for item in value]
    return value


# meiva:note
# Mask one record immediately before its handlers run. `record["message"]` is already formatted when
# the call passed arguments, and `record["extra"]` is a fresh mapping per record, so replacing both
# leaves the caller's own objects untouched.
def redact_record(record: loguru.Record) -> None:
    message = record.get("message")
    if isinstance(message, str):
        record["message"] = redact_text(message)
    record["extra"] = _redact_mapping(record["extra"])


# meiva:note
# Make `redact_record` the process-wide patcher. Idempotent: the last call wins and every call
# installs the same function.
def install_log_redaction() -> None:
    _ = loguru.logger.configure(patcher=redact_record)

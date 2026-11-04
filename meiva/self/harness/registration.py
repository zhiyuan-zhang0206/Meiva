from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from base.host.boundary.json_values import is_object_dict

# meiva:note
# A harness registration is the record a bridge writes when it starts a provider
# runtime as an agent, and the handle inside it is the only thing the child needs
# to act as that agent. Three facts are checked on every SDK call: the row is
# attached and unexpired, the machine matches, and the calling process descends
# from the controller pid the bridge recorded.
#
# The handle is stored hashed. It is delivered through the child's environment,
# never as a workspace file: the provider sandbox can read everything and write
# only its workspace, so a file would hand the token to the very agent it
# authorises.
#
# This module is the row's value shape and its decode, with no clock and no file
# access: `expired` judges at an instant the caller supplies, and
# `decode_registration` reads an already-parsed document. The file store that reads
# and writes the row lives in `store.py`.
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md
_REQUIRED_FIELDS = frozenset(
    {
        "agent_id",
        "harness",
        "external_session_id",
        "machine",
        "controller_pid",
        "handle_sha256",
        "created_at",
    }
)
_OPTIONAL_FIELDS = frozenset({"expires_at", "status"})


class RegistrationError(RuntimeError): ...


@dataclass(frozen=True, slots=True)
class HarnessRegistration:
    agent_id: int
    harness: str
    external_session_id: str
    machine: str
    controller_pid: int
    handle_sha256: str
    created_at: str
    expires_at: str | None = None
    status: str = "attached"

    @property
    def attached(self) -> bool:
        return self.status == "attached"

    def expired(self, now: datetime) -> bool:
        if self.expires_at is None:
            return False
        return _parse(self.expires_at) <= now


class RegistrationStore(Protocol):
    def load(self, agent_id: int) -> HarnessRegistration | None: ...

    def save(self, registration: HarnessRegistration) -> None: ...

    def delete(self, agent_id: int) -> bool: ...

    def find_by_handle(self, handle: str) -> HarnessRegistration | None: ...


def handle_digest(handle: str) -> str:
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()


def decode_registration(raw: object, path: Path) -> HarnessRegistration:
    if not is_object_dict(raw):
        raise RegistrationError(f"harness registration at {path} is not an object")
    payload = raw
    unknown = set(payload) - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    missing = _REQUIRED_FIELDS - set(payload)
    if unknown or missing:
        raise RegistrationError(
            f"harness registration at {path} has unknown fields {sorted(unknown)} "
            f"and misses {sorted(missing)}"
        )
    try:
        return HarnessRegistration(
            agent_id=_int(payload, "agent_id"),
            harness=_str(payload, "harness"),
            external_session_id=_str(payload, "external_session_id"),
            machine=_str(payload, "machine"),
            controller_pid=_int(payload, "controller_pid"),
            handle_sha256=_str(payload, "handle_sha256"),
            created_at=_str(payload, "created_at"),
            expires_at=None if payload.get("expires_at") is None else _str(payload, "expires_at"),
            status=_str(payload, "status") if "status" in payload else "attached",
        )
    except (TypeError, ValueError) as exc:
        raise RegistrationError(
            f"harness registration at {path} has invalid values: {exc}"
        ) from exc


# meiva:note
# The whole number `payload[key]`; a stored registration holds exactly the types it was written with,
# so another type (a bool included) is a `TypeError` naming the key.
def _int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"{key} must be an int, got {type(value).__name__}")


# meiva:note
# The string `payload[key]`, or a `TypeError` naming the key.
def _str(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if isinstance(value, str):
        return value
    raise TypeError(f"{key} must be a str, got {type(value).__name__}")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

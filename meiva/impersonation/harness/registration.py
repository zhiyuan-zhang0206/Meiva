from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from base.paths.home import resolve_meiva_home

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
_HANDLE_PREFIX = "hx_"
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

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return _parse(self.expires_at) <= (now or datetime.now(UTC))


class RegistrationStore(Protocol):
    def load(self, agent_id: int) -> HarnessRegistration | None: ...

    def save(self, registration: HarnessRegistration) -> None: ...

    def delete(self, agent_id: int) -> bool: ...

    def find_by_handle(self, handle: str) -> HarnessRegistration | None: ...


def handle_digest(handle: str) -> str:
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()


def new_handle() -> str:
    return _HANDLE_PREFIX + os.urandom(16).hex()


class FileRegistrationStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path(self, agent_id: int) -> Path:
        if agent_id <= 0:
            raise ValueError(f"agent id must be positive, got {agent_id}")
        return self._root / f"{agent_id}.json"

    def load(self, agent_id: int) -> HarnessRegistration | None:
        path = self.path(agent_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise RegistrationError(f"harness registration at {path} is not valid JSON") from exc
        return _decode(raw, path)

    def save(self, registration: HarnessRegistration) -> None:
        path = self.path(registration.agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(registration), indent=2, sort_keys=True) + "\n"
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=".registration-", suffix=".json"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def delete(self, agent_id: int) -> bool:
        try:
            self.path(agent_id).unlink()
        except FileNotFoundError:
            return False
        return True

    def find_by_handle(self, handle: str) -> HarnessRegistration | None:
        digest = handle_digest(handle)
        for path in sorted(self._root.glob("*.json")):
            try:
                registration = _decode(json.loads(path.read_text(encoding="utf-8")), path)
            except (OSError, RegistrationError):
                continue
            if registration.handle_sha256 == digest:
                return registration
        return None


def default_store() -> FileRegistrationStore:
    return FileRegistrationStore(resolve_meiva_home() / "harness" / "registrations")


def _decode(raw: Any, path: Path) -> HarnessRegistration:
    if not isinstance(raw, dict):
        raise RegistrationError(f"harness registration at {path} is not an object")
    payload = cast("dict[str, Any]", raw)
    unknown = set(payload) - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    missing = _REQUIRED_FIELDS - set(payload)
    if unknown or missing:
        raise RegistrationError(
            f"harness registration at {path} has unknown fields {sorted(unknown)} "
            f"and misses {sorted(missing)}"
        )
    try:
        return HarnessRegistration(
            agent_id=int(payload["agent_id"]),
            harness=str(payload["harness"]),
            external_session_id=str(payload["external_session_id"]),
            machine=str(payload["machine"]),
            controller_pid=int(payload["controller_pid"]),
            handle_sha256=str(payload["handle_sha256"]),
            created_at=str(payload["created_at"]),
            expires_at=None if payload.get("expires_at") is None else str(payload["expires_at"]),
            status=str(payload.get("status", "attached")),
        )
    except (TypeError, ValueError) as exc:
        raise RegistrationError(
            f"harness registration at {path} has invalid values: {exc}"
        ) from exc


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

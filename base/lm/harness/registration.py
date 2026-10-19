from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from base.log import logger

# meiva:note
# The variable a session's handle travels in. The plaintext handle exists only in the
# child's environment: the registration document keeps its sha256, so a reader of
# `$MEIVA_HOME` cannot take over a live session, and no file inside the workspace
# carries a credential the sandbox would refuse to write.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
HANDLE_ENV = "MEIVA_HARNESS_HANDLE"

# meiva:note
# The one status a usable row carries. `meiva.impersonation.harness.attach` refuses a row
# that says anything else, and the reader owns that vocabulary: this module cannot import
# it, because the SDK surface sits above `base`, so the word is repeated here and a test
# pins the pair the moment both land.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
ATTACHED = "attached"

_REQUIRED = frozenset(
    {
        "agent_id",
        "harness",
        "external_session_id",
        "machine",
        "controller_pid",
        "handle_sha256",
        "created_at",
        "expires_at",
    }
)


# meiva:note
# One line of the controller's own bookkeeping: which agent is attached to which
# external session, on which machine, written by the process that owns the session.
# The child reads nothing from it — it gets only the handle — so this is the record a
# crash sweep and an operator both read to answer "is anything still attached?".
# `expires_at` is required here even though a reader may accept its absence: the row is
# a bearer capability, and `descends_from(controller_pid)` cannot tell a reused pid from
# the original, so only an expiry keeps a dead controller's claim from living forever.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
@dataclass(frozen=True, slots=True)
class HarnessRegistration:
    agent_id: int
    harness: str
    external_session_id: str
    machine: str
    controller_pid: int
    handle_sha256: str
    created_at: str
    expires_at: str
    status: str | None = None

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "agent_id": self.agent_id,
            "harness": self.harness,
            "external_session_id": self.external_session_id,
            "machine": self.machine,
            "controller_pid": self.controller_pid,
            "handle_sha256": self.handle_sha256,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if self.status is not None:
            document["status"] = self.status
        return document


def handle_digest(handle: str) -> str:
    import hashlib

    return hashlib.sha256(handle.encode("utf-8")).hexdigest()


# meiva:note
# `$MEIVA_HOME/harness/registrations/<agent_id>.json`, owned by the bridge: it writes
# the line when the session is created, refreshes it when the session id lands, and
# deletes it when the session closes — a child process never writes or deletes one,
# because a sandbox may forbid every path outside its workspace. A process that dies
# without retiring its line leaves it to `sweep`, which drops an entry whose controller
# is gone or whose `expires_at` has passed.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
class RegistrationStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path(self, agent_id: int) -> Path:
        if agent_id <= 0:
            raise ValueError(f"agent id must be positive, got {agent_id}")
        return self._root / f"{agent_id}.json"

    def write(self, registration: HarnessRegistration) -> None:
        path = self.path(registration.agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(registration.document(), indent=2, sort_keys=True) + "\n"
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

    def read(self, agent_id: int) -> HarnessRegistration | None:
        path = self.path(agent_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError(f"registration at {path} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise TypeError(f"registration at {path} is not an object")
        payload = cast("dict[str, Any]", raw)
        missing = _REQUIRED - set(payload)
        if missing:
            raise ValueError(f"registration at {path} misses {sorted(missing)}")
        try:
            return HarnessRegistration(
                agent_id=int(payload["agent_id"]),
                harness=str(payload["harness"]),
                external_session_id=str(payload["external_session_id"]),
                machine=str(payload["machine"]),
                controller_pid=int(payload["controller_pid"]),
                handle_sha256=str(payload["handle_sha256"]),
                created_at=str(payload["created_at"]),
                expires_at=str(payload["expires_at"]),
                status=_optional_text(payload.get("status")),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"registration at {path} has invalid values: {exc}") from exc

    def delete(self, agent_id: int) -> bool:
        try:
            self.path(agent_id).unlink()
        except FileNotFoundError:
            return False
        return True

    def sweep(self, *, now: datetime | None = None) -> list[int]:
        moment = now or datetime.now(UTC)
        removed: list[int] = []
        for path in sorted(self._root.glob("*.json")):
            try:
                agent_id = int(path.stem)
            except ValueError:
                continue
            try:
                registration = self.read(agent_id)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning(
                    "[{label}] {body}",
                    label="harness",
                    event="harness_registration_unreadable",
                    body=f"leaving {path} in place: {exc!r}",
                )
                continue
            if registration is None:
                continue
            if self._stale(registration, moment):
                self.delete(agent_id)
                removed.append(agent_id)
        return removed

    def _stale(self, registration: HarnessRegistration, now: datetime) -> bool:
        return _parse(registration.expires_at) <= now or not _alive(registration.controller_pid)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# meiva:note
# `os.kill(pid, 0)` is the portable liveness probe: a signal the process never sees.
# `PermissionError` means it exists under another user, i.e. alive. A pid reused by an
# unrelated process reads as alive, which leaves one stale line for an operator rather
# than deleting a live session's — the safe direction for a record that gates attachment.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

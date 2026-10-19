from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

_REQUIRED_FIELDS = frozenset({"agent_id", "session_id", "cwd", "model", "created_at", "updated_at"})
_FIELDS = _REQUIRED_FIELDS | {"resumes"}


@dataclass(frozen=True, slots=True)
class SessionRecord:
    agent_id: int
    session_id: str
    cwd: str
    model: str
    created_at: str
    updated_at: str
    resumes: int = 0


class SessionRegistry(Protocol):
    def load(self, agent_id: int) -> SessionRecord | None: ...

    def save(self, record: SessionRecord) -> None: ...

    def delete(self, agent_id: int) -> bool: ...


# meiva:note
# The agent id is the durable identity and the SDK session id is the transcript it
# resumption needs, so the pair is written as soon as the SDK reports its init and
# refreshed on every turn. One document per agent keeps writers from different
# agents apart, and the write is a temp file plus `os.replace` so a reader never
# sees a half-written record. A corrupt document raises instead of silently
# resuming a fresh session under an id that already has history.
# meiva:link base/lm/claude_sdk/docs/claude_sdk.meiva.okf.md
class FileSessionRegistry:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path(self, agent_id: int) -> Path:
        if agent_id <= 0:
            raise ValueError(f"agent id must be positive, got {agent_id}")
        return self._root / f"{agent_id}.json"

    def load(self, agent_id: int) -> SessionRecord | None:
        path = self.path(agent_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError(f"session record at {path} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise TypeError(f"session record at {path} is not an object")
        payload = cast("dict[str, Any]", raw)
        unknown = set(payload) - _FIELDS
        missing = _REQUIRED_FIELDS - set(payload)
        if unknown or missing:
            raise ValueError(
                f"session record at {path} has unknown fields {sorted(unknown)} "
                f"and misses {sorted(missing)}"
            )
        try:
            return SessionRecord(
                agent_id=int(payload["agent_id"]),
                session_id=str(payload["session_id"]),
                cwd=str(payload["cwd"]),
                model=str(payload["model"]),
                created_at=str(payload["created_at"]),
                updated_at=str(payload["updated_at"]),
                resumes=int(payload.get("resumes", 0)),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"session record at {path} has invalid values: {exc}") from exc

    def save(self, record: SessionRecord) -> None:
        path = self.path(record.agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(record), indent=2, sort_keys=True) + "\n"
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".json")
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

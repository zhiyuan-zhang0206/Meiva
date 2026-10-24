from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from base.paths.home import resolve_meiva_home
from meiva.self.harness.registration import (
    HarnessRegistration,
    RegistrationError,
    decode_registration,
    handle_digest,
)

_HANDLE_PREFIX = "hx_"


def new_handle() -> str:
    return _HANDLE_PREFIX + os.urandom(16).hex()


# meiva:note
# The file store behind `RegistrationStore`: one JSON document per agent under
# `$MEIVA_HOME/harness/registrations/`. The decode and the row's shape live in
# `registration.py`; this class only reads and writes the files.
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md
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
        return decode_registration(raw, path)

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
                registration = decode_registration(
                    json.loads(path.read_text(encoding="utf-8")), path
                )
            except (OSError, RegistrationError):
                continue
            if registration.handle_sha256 == digest:
                return registration
        return None


def default_store() -> FileRegistrationStore:
    return FileRegistrationStore(resolve_meiva_home() / "harness" / "registrations")

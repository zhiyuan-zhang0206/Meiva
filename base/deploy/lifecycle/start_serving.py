"""Generation-owned serving state for one ``meiva start`` attempt.

Recovery cannot revive work until ordinary start proves the exact root and runtime
are serving. Native IPC binds this receipt to the root that answered readiness.  A new
attempt first publishes ``starting`` with a fresh generation; only the matching
attempt can publish ``serving`` after its readiness gate succeeds.  Therefore a
marker left by an earlier boot cannot admit recovery during the next boot.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self, TypeGuard

import psutil
from pydantic import model_validator

from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.host.runtime_interpreter import LoadedRuntimeIdentity
from base.host.verified_file.reader import regular_bytes
from base.native_process.evidence import Digest, EvidenceModel, ExpectedProcess
from base.native_process.os_platform import file_lock
from base.native_process.ownership import OwnedProcess
from base.paths import run_dir

_log = logging.getLogger("base.deploy.lifecycle.start_serving")
_STATE_FILENAME = "start-serving.json"
_LOCK_FILENAME = "start-serving.lock"
_WRITER = AtomicWriter(SYSTEM_FS)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The serving state of the unit at `home`, persisted across service processes.
def state_path(home: Path) -> Path:
    return run_dir(home) / _STATE_FILENAME


def _lock_path(home: Path) -> Path:
    return run_dir(home) / _LOCK_FILENAME


class RootBirth(EvidenceModel):
    """Local birth evidence only; never a fleet publication or resource fence."""

    home: str
    process: ExpectedProcess
    launch_digest: Digest
    runtime: LoadedRuntimeIdentity

    def same_generation(self, other: RootBirth) -> bool:
        own = OwnedProcess(self.process.pid, self.process.create_time, self.process.starttime)
        actual = OwnedProcess(other.process.pid, other.process.create_time, other.process.starttime)
        return (
            own.same_birth(actual)
            and self.home == other.home
            and self.launch_digest == other.launch_digest
            and self.runtime == other.runtime
        )


class ServingState(EvidenceModel):
    schema_version: Literal[2] = 2
    state: Literal["starting", "serving"]
    generation: str
    birth: RootBirth | None = None

    @model_validator(mode="after")
    def valid_phase(self) -> Self:
        uuid.UUID(self.generation)
        if (self.state == "serving") != (self.birth is not None):
            raise ValueError("serving requires exact root birth evidence")
        return self


def _read_state(home: Path) -> ServingState | None:
    try:
        return ServingState.model_validate_json(regular_bytes(state_path(home)))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _log.warning("[start-serving] cannot trust serving state: %s", exc)
        return None


def observe_root(home: Path) -> RootBirth:
    from base.native_process.root_control.client import (
        RootClient,
        RootClientError,
        native_identity,
    )

    home = home.resolve(strict=True)
    response = RootClient(home / "run/meiva-root/meiva-root.sock", timeout=2).status()
    body = response.get("result")
    if response["ok"] is not True or not _is_str_dict(body):
        raise RootClientError("root did not provide its birth evidence")
    root = body.get("root")
    if not _is_str_dict(root):
        raise RootClientError("root omitted its running identity")
    if root.get("home") != str(home):
        raise RootClientError("root birth belongs to another home")
    if root.get("running") is not True:
        raise RootClientError("root is not running")
    process = native_identity(root)
    if not process.live():
        raise RootClientError("root birth ended before serving observation")
    return RootBirth.model_validate(
        {
            "home": str(home),
            "process": {
                "pid": process.pid,
                "create_time": process.birth,
                "starttime": process.starttime,
            },
            "launch_digest": root.get("launch_digest"),
            "runtime": root.get("runtime"),
        }
    )


def born_identity(home: Path) -> ServingState | None:
    """Return only current exact root evidence; missing/unknown never permits."""
    state = _read_state(home)
    if state is None or state.state != "serving" or state.birth is None:
        return None
    try:
        if state.birth.same_generation(observe_root(home)) and _read_state(home) == state:
            return state
        return None
    except (OSError, psutil.Error, ValueError, RuntimeError) as exc:
        _log.warning("[start-serving] cannot observe serving root: %s", exc)
        return None


def _sync_parent_or_log(path: Path, *, writer: AtomicWriter) -> None:
    try:
        writer.fsync_parent(path)
    except OSError:
        # The replace or unlink is already visible. Preserve the safe state
        # transition rather than reporting a false failure to the caller.
        _log.warning("[start-serving] directory fsync failed after marker change", exc_info=True)


def write_state(home: Path, state: ServingState, *, writer: AtomicWriter) -> None:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.write_text(
        path, state.model_dump_json(), encoding="utf-8", mode=0o600, prefix=".start-serving-"
    )
    _sync_parent_or_log(path, writer=writer)


def begin_start(home: Path) -> str:
    """Invalidate any previous success and return this start's generation."""
    generation = str(uuid.uuid4())
    with file_lock(_lock_path(home)):
        write_state(home, ServingState(state="starting", generation=generation), writer=_WRITER)
    return generation


def mark_serving(home: Path, generation: str, *, runtime: LoadedRuntimeIdentity) -> bool:
    """Publish serving only when ``generation`` still owns the start attempt."""
    with file_lock(_lock_path(home)):
        current = _read_state(home)
        if current is None or current.state != "starting" or current.generation != generation:
            return False
        birth = observe_root(home)
        if birth.runtime != runtime:
            raise RuntimeError("serving root differs from the admitted loaded runtime")
        write_state(
            home,
            ServingState(state="serving", generation=generation, birth=birth),
            writer=_WRITER,
        )
        return True


def is_serving(home: Path) -> bool:
    """Whether a completed current-generation start permits recovery actions."""
    return born_identity(home) is not None


@contextmanager
def recovery_permitted(home: Path) -> Generator[bool]:
    """Authorize one recovery action while excluding a new start or stop.

    The caller keeps this context through the actual revive or launch. That
    makes the serving check and its effect one indivisible operation relative
    to the start generation change, rather than a racy check-then-act pair.
    """
    with file_lock(_lock_path(home)):
        yield born_identity(home) is not None


def clear_serving(home: Path) -> None:
    """Remove this unit's serving authority before a deliberate stop."""
    with file_lock(_lock_path(home)):
        path = state_path(home)
        path.unlink(missing_ok=True)
        _sync_parent_or_log(path, writer=_WRITER)

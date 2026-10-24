"""Private staging and per-kind concurrency for scheduled backup workers."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.host.atomic_io import write_text_atomic
from base.native_process.os_platform import LockTimeoutError
from services.backup.scheduler.operation.cleanup_plan import (
    Entry,
    Removal,
    artifact_removals,
    scratch_removal,
)

TERMINATE_GRACE_S = 3.0


# meiva:note
# One scheduled job's private scratch root and what its cleanup must remove. The three
# declarations are values the cleanup executes: `staged_artifacts` keeps only complete
# encrypted artifacts under `artifact/`, `plaintext_scratch` removes `scratch/`, and
# `throwaway` reaps the throwaway clusters a killed worker left behind.
@dataclass(frozen=True)
class OperationKind:
    name: str
    control_root: Path
    staged_artifacts: bool = False
    plaintext_scratch: bool = False
    throwaway: ThrowawayConfig | None = None
    grace_s: float = TERMINATE_GRACE_S


class OperationDeferred(RuntimeError):  # noqa: N818 -- a clean scheduling outcome
    """The worker declined before preparing an artifact."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"operation deferred ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


class OperationBusyError(LockTimeoutError):
    """Another scheduled operation holds this kind's concurrency lock."""


def publish_result(path: Path, result: Mapping[str, object]) -> None:
    """Publish a complete private request or result, never a process receipt."""
    write_text_atomic(
        path,
        json.dumps(result, sort_keys=True, separators=(",", ":")),
        mode=0o600,
        sync_parent=True,
    )


def _entry(path: Path) -> Entry:
    return Entry(
        path.name, is_file=path.is_file(), is_dir=path.is_dir(), is_symlink=path.is_symlink()
    )


def _remove(directory: Path, removal: Removal) -> None:
    target = directory / removal.name
    if removal.tree:
        shutil.rmtree(target)
    else:
        target.unlink()


# meiva:note
# Remove private staging; retain its path and raise if sanitization fails. Plaintext and
# partial pipeline files go first, so a failing last step leaves no plaintext behind.
def cleanup(kind: OperationKind, work: Path) -> None:
    try:
        if kind.staged_artifacts and (work / "artifact").is_dir():
            artifacts = work / "artifact"
            for removal in artifact_removals([_entry(item) for item in artifacts.iterdir()]):
                _remove(artifacts, removal)
        if kind.plaintext_scratch:
            removal = scratch_removal(_entry(work / "scratch"))
            if removal is not None:
                _remove(work, removal)
        if kind.throwaway is not None:
            # meiva:note
            # Imported where used: the scheduler daemon imports this module and must not
            # carry the throwaway cluster tooling until a restore drill reaps one.
            from base.cluster.dataplane.pg_tools import sweep_orphaned_throwaway_clusters

            sweep_orphaned_throwaway_clusters(config=kind.throwaway)
        shutil.rmtree(work)
    except Exception as exc:
        exc.add_note(f"backup staging cleanup failed; inspect private directory {work}")
        raise

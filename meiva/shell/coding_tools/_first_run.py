"""Claude Code first-run presets — an unattended spawn must never park on a dialog.

The Claude launcher runs ``preset_claude_first_run`` before any session starts.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import assert_never

from base.host.atomic_io import write_text_atomic
from meiva.shell.coding_tools.plans.first_run import (
    CONFIG_PRESET,
    SETTINGS_PRESET,
    FilePlan,
    plan_preset,
    plan_trust,
)


# meiva:note
# Copy `path` to a uniquely named sibling backup; same-second callers never collide, whatever `stamp` they share.
def _backup_before_write(path: Path, stamp: str) -> Path:
    fd, raw_backup = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.bak-{stamp}-")
    os.close(fd)
    backup = Path(raw_backup)
    backup.write_bytes(path.read_bytes())
    return backup


# meiva:note
# Publish `data` at `path` through a unique temporary file and an atomic replace, so two concurrent writers in the same second cannot collide; an existing file keeps its permission bits.
def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", mode=mode
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


# meiva:note
# Run one file plan: refuse (after a backup) raises its problem, keep prints `kept`, write backs an existing file up when `backup` is set and publishes the merged document.
def _apply(
    plan: FilePlan, path: Path, stamp: str, *, backup: bool, kept: str, written: str
) -> None:
    match plan.action:
        case "refuse":
            _backup_before_write(path, stamp)
            raise RuntimeError(plan.problem)
        case "keep":
            print(kept)
        case "write":
            if backup and path.exists():
                _backup_before_write(path, stamp)
            _write_json_atomic(path, plan.data)
            print(written)
        case _:
            assert_never(plan.action)


# meiva:note
# Mark `workspace` trusted in `<home>/.claude.json`. An unparsable file is backed up and raises instead of being overwritten.
def pretrust(home: Path, workspace: Path, stamp: str) -> None:
    path = home / ".claude.json"
    key = workspace.as_posix()
    plan = plan_trust(_read(path), key, "~/.claude.json")
    _apply(
        plan,
        path,
        stamp,
        backup=False,
        kept=f"(already trusted: {key})",
        written=f"+ trusted: {key}",
    )


# meiva:note
# Preset Claude Code's first-run dialogs so an unattended spawn never parks on one: `~/.claude/settings.json` and `~/.claude.json` under `home`. Each file is backed up (under `stamp`) before its first change, a second call is a no-op, and an unparsable file is backed up and raises instead of being overwritten. One line per file: `+ preset` / `(already preset`.
def preset_claude_first_run(home: Path, stamp: str) -> None:
    for relative, updates, label in (
        (Path(".claude") / "settings.json", SETTINGS_PRESET, "~/.claude/settings.json"),
        (Path(".claude.json"), CONFIG_PRESET, "~/.claude.json"),
    ):
        path = home / relative
        _apply(
            plan_preset(_read(path), updates, label),
            path,
            stamp,
            backup=True,
            kept=f"(already preset: {label})",
            written=f"+ preset: {label}",
        )

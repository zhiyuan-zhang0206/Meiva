from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, TypeGuard

BUILD_STATE_NAME = "build-state.json"


class BuildState(TypedDict):
    source_hash: str
    dr: str
    signed_at: str


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def read_build_state(path: Path) -> BuildState | None:
    try:
        raw: object = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not _is_str_dict(raw):
        return None
    try:
        source_hash = raw["source_hash"]
        dr = raw["dr"]
        signed_at = raw["signed_at"]
    except KeyError:
        return None
    if (
        not isinstance(source_hash, str)
        or not isinstance(dr, str)
        or not isinstance(signed_at, str)
    ):
        return None
    return BuildState(source_hash=source_hash, dr=dr, signed_at=signed_at)


def write_build_state(path: Path, source_hash: str, dr: str, *, signed_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = BuildState(
        source_hash=source_hash,
        dr=dr,
        signed_at=signed_at,
    )
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

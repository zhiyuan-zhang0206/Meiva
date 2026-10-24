"""Disk-backed IM Bridge switch state."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import TypeAdapter

_log = logging.getLogger("services.entrypoints.im_bridge.state")


# meiva:note
# Per-chat switch persistence under `state_dir` survives daemon restarts and updates.
def _switch_state_path(state_dir: Path) -> Path:
    path = state_dir / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# meiva:note
# Restore {channel}:{chat_id} -> agent_id from disk; {} when absent.
def load_switch_state(state_dir: Path) -> dict[str, int]:
    path = _switch_state_path(state_dir)
    if not path.exists():
        return {}
    return TypeAdapter(dict[str, int]).validate_json(path.read_text(encoding="utf-8"), strict=True)


# meiva:note
# Persist atomically so a crash never leaves a half-written file.
def save_switch_state(state_dir: Path, state: dict[str, int]) -> None:
    path = _switch_state_path(state_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

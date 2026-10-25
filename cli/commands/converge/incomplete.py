from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig

# meiva:note
# A converge that aborts leaves this marker in the home it was converging. It
# exists because the failure is invisible otherwise: `meiva start` brings the
# services up after the aborted converge, so the host looks healthy while one
# step never applied. One marker per home; only a fully successful converge on
# that home clears it.
# meiva:link cli/commands/converge/docs/converge-incomplete.meiva.okf.md
INCOMPLETE_NAME = "converge-incomplete.json"


@dataclass(frozen=True)
class IncompleteConverge:
    at: str
    step: str
    error: str

    def summary(self) -> str:
        return f"{self.at} at step {self.step!r}: {self.error}"


def state_path(meiva_home: Path) -> Path:
    return meiva_home / "state" / INCOMPLETE_NAME


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def read_incomplete(meiva_home: Path) -> IncompleteConverge | None:
    try:
        raw: object = json.loads(state_path(meiva_home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data = raw if _is_object_dict(raw) else {}
    try:
        return IncompleteConverge(
            at=str(data["at"]), step=str(data["step"]), error=str(data["error"])
        )
    except KeyError:
        return None


def record_incomplete(meiva_home: Path, *, step: str, error: str) -> None:
    path = state_path(meiva_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = Clock(
        ClockConfig("UTC", None, message_timestamp_weekday=False), timers=SYSTEM_TIMERS
    ).now()
    payload = {
        "at": now.isoformat(timespec="seconds"),
        "step": step,
        "error": error,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_incomplete(meiva_home: Path) -> None:
    state_path(meiva_home).unlink(missing_ok=True)

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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


def read_incomplete(meiva_home: Path) -> IncompleteConverge | None:
    try:
        raw: Any = json.loads(state_path(meiva_home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    try:
        return IncompleteConverge(
            at=str(data["at"]), step=str(data["step"]), error=str(data["error"])
        )
    except KeyError:
        return None


def record_incomplete(meiva_home: Path, *, step: str, error: str) -> None:
    path = state_path(meiva_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "step": step,
        "error": error,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_incomplete(meiva_home: Path) -> None:
    state_path(meiva_home).unlink(missing_ok=True)

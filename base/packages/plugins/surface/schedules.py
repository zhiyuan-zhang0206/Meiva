# meiva:note
# Pure surface dataclasses for plugin schedules and dynamic status-panel cards.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

import re
from dataclasses import dataclass

_SECRET_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_STAT_CARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _require_stripped_str(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"ScheduleSpec.{field} must be a non-empty stripped string, got {value!r}")


def _validate_secret_vars(vars_tuple: tuple[str, ...]) -> None:
    seen_secrets: set[str] = set()
    for var in vars_tuple:
        if not isinstance(var, str) or not _SECRET_ENV_VAR_RE.match(var):
            raise ValueError(
                f"ScheduleSpec.secret_env_vars entry {var!r} must match ^[A-Z][A-Z0-9_]*$"
            )
        if var.startswith("MEIVA_"):
            raise ValueError(
                f"ScheduleSpec.secret_env_vars entry {var!r} must not start with 'MEIVA_'"
            )
        if var in seen_secrets:
            raise ValueError(f"ScheduleSpec.secret_env_vars contains duplicate entry {var!r}")
        seen_secrets.add(var)


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    machine: str
    description: str
    script: str
    command: str = "python schedule.py"
    secret_env_vars: tuple[str, ...] = ()
    default_enabled: bool = True

    def __post_init__(self) -> None:
        _require_stripped_str(self.name, "name")
        _require_stripped_str(self.machine, "machine")
        _require_stripped_str(self.description, "description")
        if not isinstance(self.script, str) or not self.script.strip():
            raise ValueError("ScheduleSpec.script must be a non-empty string")
        _require_stripped_str(self.command, "command")
        if not isinstance(self.secret_env_vars, tuple):
            raise TypeError(
                f"ScheduleSpec.secret_env_vars must be a tuple, got {type(self.secret_env_vars).__name__}"
            )
        _validate_secret_vars(self.secret_env_vars)
        if not isinstance(self.default_enabled, bool):
            raise TypeError(
                f"ScheduleSpec.default_enabled must be a boolean, got {self.default_enabled!r}"
            )


@dataclass(frozen=True)
class StatCardSpec:
    id: str
    label: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not (1 <= len(self.id) <= 64)
            or not _STAT_CARD_ID_RE.match(self.id)
        ):
            raise ValueError(
                f"StatCardSpec.id must match ^[a-z0-9][a-z0-9_-]*$ and have length 1..64, got {self.id!r}"
            )
        if (
            not isinstance(self.label, str)
            or not (1 <= len(self.label) <= 80)
            or self.label != self.label.strip()
        ):
            raise ValueError(
                f"StatCardSpec.label must be a non-empty stripped string of length 1..80, got {self.label!r}"
            )

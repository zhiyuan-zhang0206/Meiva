# meiva:note
# Pure surface dataclasses for plugin schedules and dynamic status-panel cards.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _validate_schedule_shape(spec: ScheduleSpec) -> None:
    _require_stripped_str(spec.name, "name")
    _require_stripped_str(spec.machine, "machine")
    _require_stripped_str(spec.description, "description")
    if not isinstance(spec.script, str) or not spec.script.strip():
        raise ValueError("ScheduleSpec.script must be a non-empty string")
    _require_stripped_str(spec.command, "command")
    if not isinstance(spec.secret_env_vars, tuple):
        raise TypeError(
            f"ScheduleSpec.secret_env_vars must be a tuple, got {type(spec.secret_env_vars).__name__}"
        )
    _validate_secret_vars(spec.secret_env_vars)
    if not isinstance(spec.default_enabled, bool):
        raise TypeError(
            f"ScheduleSpec.default_enabled must be a boolean, got {spec.default_enabled!r}"
        )


def _validate_recurring_trigger(spec: ScheduleSpec) -> None:
    if spec.trigger_kind not in ("cron", "every"):
        raise ValueError("trigger_kind must be 'cron' or 'every'")
    if spec.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if spec.timezone is not None:
        try:
            ZoneInfo(spec.timezone)
        except (ZoneInfoNotFoundError, KeyError) as exc:
            raise ValueError(f"invalid IANA timezone: {spec.timezone!r}") from exc
    if spec.trigger_kind == "cron":
        if not spec.cron_expr or spec.interval_seconds is not None:
            raise ValueError("cron trigger requires cron_expr only")
        return
    if spec.cron_expr is not None or spec.interval_seconds is None:
        raise ValueError("every trigger requires interval_seconds only")
    if spec.interval_seconds < 30:
        raise ValueError("interval_seconds must be at least 30")


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    machine: str
    description: str
    script: str
    trigger_kind: Literal["cron", "every"]
    command: str = "python schedule.py"
    secret_env_vars: tuple[str, ...] = ()
    default_enabled: bool = True
    cron_expr: str | None = None
    interval_seconds: int | None = None
    timezone: str | None = None
    timeout_seconds: int = 600

    def __post_init__(self) -> None:
        _validate_schedule_shape(self)
        _validate_recurring_trigger(self)


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

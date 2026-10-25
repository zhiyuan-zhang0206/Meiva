# meiva:note
# Boot-lite value parsing: one raw env string to the typed value of a table field.
#
# `parse_field` mirrors pydantic's non-strict coercion for the field types the generated
# table (`base/host/env/config_lite_table.py`) declares, so a boot-path field validates at
# import without constructing `Settings`. Values in, values or a `ValueError` out.


from __future__ import annotations

import re
from pathlib import Path

__all__ = ["FieldValue", "parse_field"]

type FieldValue = str | int | float | bool | Path | list[str]

# meiva:note
# The parse kinds cover the table's field types (bool strings, int from a trimmed integer
# literal or an integral decimal, float, Path, the CSV split of the eval allowlist). The
# parity test (base/tests/config/bootstrap/test_config_lite_parity.py) locks every kind
# against the eager sub-model construction, error for error.

_BOOL_TRUE = frozenset({"1", "true", "yes", "on", "t", "y"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off", "f", "n"})
# meiva:note
# Python's `int()` accepts the optional sign and underscores; pydantic's int
# additionally accepts a plain decimal that is integral ("5.0", not "5.", not
# ".5", not "5e1") — and rejects non-ASCII digits (Python would accept them).
_INT_LITERAL = re.compile(r"[+-]?[0-9][0-9_]*\Z")
_INT_DECIMAL = re.compile(r"[+-]?[0-9]+\.[0-9]+\Z")


def _parse_bool(raw: str) -> bool:
    lowered = raw.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise ValueError("not a boolean")


def _parse_int(raw: str) -> int:
    text = raw.strip()
    if _INT_LITERAL.match(text):
        return int(text)
    if _INT_DECIMAL.match(text):
        value = float(text)
        if value.is_integer():
            return int(value)
    raise ValueError("not an integer")


# meiva:note
# Mirror of GeneralSettings._validate_timezone (same message).
def _check_iana(alias: str, value: FieldValue) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(value, str):
        raise TypeError(f"{alias} requires a string timezone name")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"{alias}={value!r} is not a valid IANA timezone name "
            f"(e.g. America/Los_Angeles, Asia/Shanghai, UTC)"
        ) from exc


# meiva:note
# Mirror of telemetry_otlp_port's Field(ge=1, le=65535).
def _check_port(alias: str, value: FieldValue) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{alias}={value!r} must be a TCP port between 1 and 65535")


# meiva:note
# Mirror of AgentEvalSettings._validate_eval_network_allowlist (same message).
def _check_eval_allowlist(alias: str, value: FieldValue) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{alias} requires a list of allowlist entries")
    unsupported = sorted(set(value) - {"web", "understand"})
    if unsupported:
        raise ValueError(
            f"{alias}: eval network allowlist only accepts 'web' and 'understand'; "
            f"unsupported entries: {unsupported}"
        )


_CHECKS = {
    "iana": _check_iana,
    "port": _check_port,
    "eval_allowlist": _check_eval_allowlist,
}
_PARSE_KINDS = frozenset({"str", "int", "float", "bool", "bool_or_none", "path", "csv_str_list"})


# meiva:note
# Coerce one env value to the field's type — ValueError on a bad value.
def parse_field(name: str, alias: str, kind: str, check: str | None, raw: str) -> FieldValue:
    if kind not in _PARSE_KINDS:
        raise ValueError(f"unknown parse kind {kind!r}")
    try:
        if kind == "str":
            value: FieldValue = raw
        elif kind == "int":
            value = _parse_int(raw)
        elif kind == "float":
            value = float(raw.strip())
        elif kind in ("bool", "bool_or_none"):
            value = _parse_bool(raw)
        elif kind == "path":
            value = Path(raw)
        else:
            value = [item.strip() for item in raw.split(",") if item.strip()]
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{alias}={raw!r} is not a valid {kind} for config field {name!r} "
            f"(boot-lite parse; the field type is declared in base/config/)"
        ) from exc
    if check is not None:
        _CHECKS[check](alias, value)
    return value

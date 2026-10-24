# meiva:note
# What the `.env` audit may keep of a configuration change, as values.
#
# A change survives with its old and new value only for an alias the field declarations
# register as `sensitive: false`. A sensitive alias, and an alias nothing registers, keeps its
# name alone, so the record can still say "this key changed" without becoming a second place
# secrets live. The shell reads the declarations from the config registry and the process from
# psutil; everything here is a function of those values.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

__all__ = [
    "AliasMeta",
    "FieldDeclaration",
    "RedactedChange",
    "alias_metadata",
    "process_label",
    "redact_changes",
]


# meiva:note
# The scope and sensitivity a config field declares for its `.env` alias.
class AliasMeta(NamedTuple):
    scope: str
    sensitive: bool


# meiva:note
# One config field as its declaration states it. `sensitive` is whatever the declaration
# carries, so a non-boolean value is rejected here and not coerced.
@dataclass(frozen=True)
class FieldDeclaration:
    name: str
    alias: str
    scope: str
    sensitive: bool | int | float | str | None


# meiva:note
# Project the declarations onto their `.env` aliases.
#
# Raises:
#     TypeError: a field's `sensitive` flag is not a boolean, so redaction could not trust it.
#     ValueError: two fields declare the same alias, so one alias would mean two policies.
def alias_metadata(declarations: Sequence[FieldDeclaration]) -> dict[str, AliasMeta]:
    metadata: dict[str, AliasMeta] = {}
    for declaration in declarations:
        if not isinstance(declaration.sensitive, bool):
            raise TypeError(
                f"config field {declaration.name!r} must declare a boolean sensitive flag"
            )
        if declaration.alias in metadata:
            raise ValueError(f"config fields declare duplicate env alias {declaration.alias!r}")
        metadata[declaration.alias] = AliasMeta(declaration.scope, declaration.sensitive)
    return metadata


# meiva:note
# One redacted `{alias, old, new}` entry of a write record.
#
# `scope` and `sensitive` are None for an alias no field registers; `old` and `new` are None
# unless the alias is registered as not sensitive.
@dataclass(frozen=True)
class RedactedChange:
    alias: str
    scope: str | None
    sensitive: bool | None
    old: str | None
    new: str | None

    def as_record(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "scope": self.scope,
            "sensitive": self.sensitive,
            "old": self.old,
            "new": self.new,
        }


# meiva:note
# The `old` or `new` side of a raw diff entry: an environment value is a string, or None when the
# alias was absent on that side.
def _env_value(change: Mapping[str, object], side: str) -> str | None:
    value = change.get(side)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"env change {change.get('alias')!r} carries a non-string {side} value")
    return value


# meiva:note
# Redact a raw `{alias, old, new}` diff into its auditable form.
def redact_changes(
    changes: Sequence[Mapping[str, object]], metadata: Mapping[str, AliasMeta]
) -> tuple[RedactedChange, ...]:
    redacted: list[RedactedChange] = []
    for change in changes:
        alias = str(change["alias"])
        meta = metadata.get(alias)
        recordable = meta is not None and meta.sensitive is False
        redacted.append(
            RedactedChange(
                alias=alias,
                scope=meta.scope if meta is not None else None,
                sensitive=meta.sensitive if meta is not None else None,
                old=_env_value(change, "old") if recordable else None,
                new=_env_value(change, "new") if recordable else None,
            )
        )
    return tuple(redacted)


# meiva:note
# The process name and the command line a record may carry: the executable only, never an
# argument, because a configuration value can ride on one (`python -c ... <secret>`).
def process_label(name: str, cmdline: Sequence[str]) -> tuple[str, str]:
    executable = Path(cmdline[0]).name if cmdline else name
    return name, f"{executable} [arguments redacted]"[:200]

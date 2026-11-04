# meiva:note
# The machine dimension every audit row carries: a canonical machine name, or `unknown`.
#
# Before this module the dimension had three states the data could not tell apart. A process
# whose identity reader raised `MachineNameMissing` silently wrote `socket.gethostname()`
# instead, so 741 `audit_events` rows over two weeks carried `Zyons-Mac-mini.local` and three
# sibling hostnames next to `macmini` / `company-air`. Every reader that grouped by `machine`
# counted one host as two, and the substitution left no trace.
#
# Three states, three shapes in the data: a resolved name is the canonical alias, an unreadable
# identity is `unknown` with one warning per process, and the observed host is the diagnostic
# column `machine_hostname` -- never the identity.
#
# A value this module cannot map is passed through unchanged, and `unknown` is never guessed
# into an alias. The mapping and its evidence live in `aliases.py`.
# meiva:link base/docs/configuration/configuration.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

from base.telemetry.machine.aliases import MACHINES

__all__ = ["UNKNOWN_MACHINE", "CanonicalMachine", "canonical_machine", "machine_aliases"]

UNKNOWN_MACHINE = "unknown"


# meiva:note
# What a raw value means: its canonical form and whether the mapping recognized it. `raw` is kept
# so a caller can record what it saw without re-reading the row.
@dataclass(frozen=True)
class CanonicalMachine:
    value: str
    mapped: bool
    raw: str


# meiva:note
# The mapping is a module constant: immutable, no environment, no file read at run time, and the
# same answer for every caller in the process.
def machine_aliases() -> dict[str, str]:
    return dict(MACHINES)


# meiva:note
# Map one raw value to its canonical form, or pass it through unchanged. `unknown` is returned
# as-is and is never mapped to a machine: the point of the sentinel is that nobody knows which
# host produced the row.
def canonical_machine(raw: str) -> CanonicalMachine:
    value = raw.strip()
    if not value or value == UNKNOWN_MACHINE:
        return CanonicalMachine(value=UNKNOWN_MACHINE, mapped=False, raw=raw)
    alias = MACHINES.get(value)
    if alias is None:
        return CanonicalMachine(value=value, mapped=False, raw=raw)
    return CanonicalMachine(value=alias, mapped=True, raw=raw)

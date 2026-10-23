# meiva:note
# Which note each service of `meiva status` carries, and the table's layout.
#
# Values in, text out. The shell reads the roster (the capability gate reasons) and the
# last start's manifest, and hands over plain values; this module decides what the service
# table says about each service and how wide its first column is.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["NOT_LAUNCHED_NOTE", "RosterEntry", "ServiceTable", "service_table"]

NOT_LAUNCHED_NOTE = "not launched by the last meiva start"
_SERVICE_COLUMN = "service"


# meiva:note
# One service of this host's roster, as the plain values the table needs.
@dataclass(frozen=True)
class RosterEntry:
    # meiva:note
    # The roster's key for the service (`ServiceSpec.session`): what a root manifest names.
    session: str
    # meiva:note
    # The composed name the table prints (`meiva-<session>`).
    name: str
    # meiva:note
    # Why the capability gate drops the service from the start roster; None when it would run.
    gate_reason: str | None


# meiva:note
# The table's heading and, per roster entry in order, the note its row carries.
@dataclass(frozen=True)
class ServiceTable:
    header: str
    rule: str
    name_width: int
    notes: tuple[str | None, ...]


# meiva:note
# Explain every service excluded by capabilities or not launched by the last start.
# Show this host's role roster WITH each gated-out service's reason, so a service the
# start path drops (meiva-browser with no display) is visible and explained rather than
# silently missing: `meiva status` is the first diagnostic command and must not go blind
# on the broken service. A service the capability gate keeps but the last start did not
# put in the root manifest reads "not launched by the last meiva start"; with no manifest
# yet (`launched` is None) nothing was launched, so every kept service reads that way.
def service_table(
    entries: tuple[RosterEntry, ...], launched: frozenset[str] | None
) -> ServiceTable:
    name_width = max((len(entry.name) for entry in entries), default=len(_SERVICE_COLUMN))
    header = f"{_SERVICE_COLUMN.ljust(name_width)}  root  probe"
    return ServiceTable(
        header=header,
        rule="-" * len(header),
        name_width=name_width,
        notes=tuple(_note(entry, launched) for entry in entries),
    )


def _note(entry: RosterEntry, launched: frozenset[str] | None) -> str | None:
    if entry.gate_reason is not None:
        return entry.gate_reason
    if launched is not None and entry.session in launched:
        return None
    return NOT_LAUNCHED_NOTE

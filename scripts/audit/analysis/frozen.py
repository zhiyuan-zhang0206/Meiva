# meiva:note
# Which tracked paths are frozen history: point-in-time records never rewritten.
#
# `where_used.py` reports a reference in them as its own group instead of live content,
# and `module_moves.py` leaves them out of the universe it audits.

from __future__ import annotations

__all__ = ["FROZEN_PREFIXES", "is_frozen"]

FROZEN_PREFIXES = ("docs/decisions/", "docs/postmortems/")


def is_frozen(name: str) -> bool:
    # meiva:note
    # Whether a tracked path is in a frozen history axis: never rewritten.
    return name.startswith(FROZEN_PREFIXES)

# meiva:note
# Which results the exec child delivers telemetry for, and which of them a failed delivery
# rewrites. A delivery that comes back unfinished is a warning and never changes the envelope.
# A delivery that raises turns a success into a crash envelope the parent can see, but never
# replaces a crash the child already reported: that crash stays primary.
# meiva:link agent/execution/docs/child-boundaries.meiva.okf.md

from __future__ import annotations

_DELIVERED_KINDS = frozenset({"done", "lifecycle", "crashed"})
_REWRITTEN_KINDS = frozenset({"done", "lifecycle"})


def delivers_telemetry(kind: str) -> bool:
    return kind in _DELIVERED_KINDS


def rewrites_result_after_delivery_failure(kind: str) -> bool:
    return kind in _REWRITTEN_KINDS

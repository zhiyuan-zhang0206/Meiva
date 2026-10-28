from __future__ import annotations

from base.lm.harness.base_session import TurnTicket
from base.lm.harness.cancel import CancelReport
from base.lm.harness.turn import SessionTotals, TurnOutcome

# meiva:note
# `SessionTotals.per_turn_usage` turns False as soon as one turn's numbers were
# unavailable, so a caller can never read a silently-zeroed total as a measured
# one. `CancelReport` (owned by `base.lm.harness.cancel`) says `settled=None` when the
# runtime pid is unobservable and names every process that outlived the sweep in `remaining`.
# meiva:link base/lm/harness/kimi/docs/kimi.meiva.okf.md
__all__ = ["CancelReport", "SessionTotals", "TurnOutcome", "TurnTicket"]

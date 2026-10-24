# meiva:note
# What an exec child can report: the kinds a result envelope may carry. The protocol writes and
# validates envelopes against this vocabulary and the delivery plan decides per kind, so both
# name the same set.
# meiva:link agent/execution/docs/child-boundaries.meiva.okf.md

from __future__ import annotations

from typing import Literal, get_args

ResultKind = Literal["done", "cancelled", "timed_out", "lifecycle", "crashed"]
RESULT_KINDS: frozenset[str] = frozenset(get_args(ResultKind))

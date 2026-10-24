# meiva:note
# What the helper-chain watch decides, as functions of values: how many consecutive broken
# readings retire the root, and the line of evidence that names the broken chain. The reads
# (the ancestor walk, the marker, the clock) belong to `helper_chain.py`.

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["ChainRound", "HelperChainConfig", "evidence_line", "observe_round"]


# meiva:note
# How often the chain is read and how many consecutive broken readings confirm it.
@dataclass(frozen=True)
class HelperChainConfig:
    interval_s: float = 5.0
    confirm_rounds: int = 2

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval_s) or self.interval_s <= 0:
            raise ValueError("interval_s must be finite and positive")
        if self.confirm_rounds < 1:
            raise ValueError("confirm_rounds must be at least 1")


# meiva:note
# The count of consecutive broken readings after one reading, and whether it confirms the break.
@dataclass(frozen=True)
class ChainRound:
    broken_rounds: int
    retire: bool


# meiva:note
# An intact reading resets the count; a broken one adds to it and retires at `confirm_rounds`.
def observe_round(*, broken_rounds: int, intact: bool, confirm_rounds: int) -> ChainRound:
    if intact:
        return ChainRound(broken_rounds=0, retire=False)
    broken = broken_rounds + 1
    return ChainRound(broken_rounds=broken, retire=broken >= confirm_rounds)


# meiva:note
# The evidence a retirement records: this root's pid, the marker pid the helper seeded (or
# its absence) and how many consecutive readings were broken.
def evidence_line(*, root_pid: int, marker: str | None, broken_rounds: int) -> str:
    return (
        f"root pid {root_pid} is no longer under the helper that seeded it "
        f"(marker pid {marker or 'absent'}, "
        f"{broken_rounds} consecutive broken readings)"
    )

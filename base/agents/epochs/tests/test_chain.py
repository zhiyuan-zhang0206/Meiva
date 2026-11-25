from __future__ import annotations

from datetime import UTC, datetime

from base.agents.epochs.chain import usable_epochs
from base.agents.epochs.model import HarnessEpoch

_T0 = datetime(2026, 11, 25, 8, tzinfo=UTC)


def _epoch(seq: int, harness: str, *, ended: bool = True) -> HarnessEpoch:
    return HarnessEpoch(
        agent_id=1,
        seq=seq,
        harness=harness,
        model=None,
        session_id=None,
        started_at=_T0,
        ended_at=_T0 if ended else None,
        source_start=None,
        source_end=None,
        switched_by="birth" if seq == 0 else "user",
        reason=None,
    )


_CHAIN = [_epoch(0, "meiva"), _epoch(1, "codex"), _epoch(2, "meiva", ended=False)]


def test_a_chain_ending_on_the_agents_harness_is_usable() -> None:
    assert usable_epochs(_CHAIN, "meiva") == _CHAIN


def test_a_chain_that_ends_elsewhere_or_is_open_at_the_wrong_end_is_not() -> None:
    assert usable_epochs(_CHAIN, "codex") == []
    assert usable_epochs(_CHAIN[:2], "codex") == []


def test_one_epoch_a_gap_or_no_rows_is_not_a_chain() -> None:
    assert usable_epochs(_CHAIN[:1], "meiva") == []
    assert usable_epochs([_CHAIN[0], _CHAIN[2]], "meiva") == []
    assert usable_epochs([], "meiva") == []

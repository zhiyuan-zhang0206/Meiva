# meiva:note
# Which stored rows a launch attempt may wake: the current attempt on a launchable harness only.

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from base.agents.birth.harness import LAUNCHABLE_HARNESSES
from base.api_contracts.agent_contract import Harness
from ops.lifecycle.launch_row import StoredLaunch, require_current_launch_row

_ATTEMPT = uuid4()


def _row(
    *,
    machine: str | None = "runner-a",
    attempt: UUID | None = _ATTEMPT,
    status: str = "idling",
    harness: str = "meiva",
) -> StoredLaunch:
    return StoredLaunch(machine, attempt, status, harness)


def _require(
    row: StoredLaunch | None, *, launchable: frozenset[Harness] = LAUNCHABLE_HARNESSES
) -> None:
    require_current_launch_row(
        row,
        agent_id=7,
        launch_attempt_id=_ATTEMPT,
        local_machine="runner-a",
        launchable=launchable,
    )


# meiva:note
# The current attempt, placed here and not yet admitted, on every harness that has a launcher.
@pytest.mark.parametrize("harness", sorted(LAUNCHABLE_HARNESSES))
def test_the_current_attempt_on_a_launchable_harness_is_accepted(harness: Harness) -> None:
    _require(_row(harness=harness.value))


# meiva:note
# A stale attempt cannot wake an identity now placed somewhere else or already past that attempt.
@pytest.mark.parametrize(
    "row",
    [
        None,
        _row(machine="runner-b"),
        _row(machine=None),
        _row(attempt=uuid4()),
        _row(attempt=None),
        _row(status="running"),
        _row(status="terminated"),
    ],
)
def test_a_stale_or_misplaced_row_is_refused(row: StoredLaunch | None) -> None:
    with pytest.raises(ValueError, match="agent 7 launch attempt is stale or misplaced"):
        _require(row)


# meiva:note
# The shipped set grows as launchers land, so the refusal is driven through the parameter rather
# than by betting on which harnesses still lack one: antigravity was this test's example until
# the release that gave it a launcher.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def test_a_harness_without_a_launcher_is_refused() -> None:
    without_it = frozenset(LAUNCHABLE_HARNESSES - {Harness.ANTIGRAVITY})

    with pytest.raises(ValueError, match="no launcher yet"):
        _require(_row(harness=Harness.ANTIGRAVITY.value), launchable=without_it)


# meiva:note
# A harness value outside the vocabulary is an error, not a launchable default.
def test_an_unknown_harness_is_an_error() -> None:
    with pytest.raises(ValueError, match="not a valid Harness"):
        _require(_row(harness="not-a-harness"))

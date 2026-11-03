# meiva:note
# The turn-end vocabulary is a closed set. A declaration outside it is refused where it
# enters the state — refused, never raised: a field that measures the agent must not fail the
# work it measures, so the exec boundary records `invalid` and the turn carries on.
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md
from __future__ import annotations

import pytest

from base.agents.observation import ended_with


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("no_input", None),
        ("done", None),
        ("budget", None),
        ("deferred:time", "waiting for the 09:00 window"),
        ("waiting:ci:8341", None),
        ("waiting:peer:6107", "its answer decides the next step"),
    ],
)
def test_accepts_every_value_of_the_closed_set(value: str, reason: str | None) -> None:
    assert ended_with.problem(value, reason=reason) is None


@pytest.mark.parametrize(
    ("value", "reason", "match"),
    [
        ("finished", None, "not one of the turn-end values"),
        ("", None, "not one of the turn-end values"),
        ("deferred", None, "not one of the turn-end values"),
        ("Waiting:ci", None, "not one of the turn-end values"),
        (7, None, "not one of the turn-end values"),
        ("waiting:", None, "names no object"),
        ("deferred:time", None, "carries no reason"),
        ("deferred:time", "   ", "carries no reason"),
    ],
)
def test_states_why_a_declaration_is_unusable(
    value: object, reason: str | None, match: str
) -> None:
    problem = ended_with.problem(value, reason=reason)
    assert problem is not None
    assert match in problem

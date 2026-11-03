# meiva:note
# The turn-end vocabulary is a closed set: a declaration outside it is refused where it
# enters the state, so no record can carry free text.
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
    assert ended_with.validate(value, reason=reason) == value


@pytest.mark.parametrize(
    ("value", "reason", "match"),
    [
        ("finished", None, "ended_with must be one of"),
        ("", None, "ended_with must be one of"),
        ("deferred", None, "ended_with must be one of"),
        ("Waiting:ci", None, "ended_with must be one of"),
        ("waiting:", None, "needs the object"),
        ("deferred:time", None, "one-sentence reason"),
        ("deferred:time", "   ", "one-sentence reason"),
    ],
)
def test_refuses_anything_outside_it(value: str, reason: str | None, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ended_with.validate(value, reason=reason)

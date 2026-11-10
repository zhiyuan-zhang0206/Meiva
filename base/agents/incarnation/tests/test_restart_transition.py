# meiva:note
# The transition a deferred restart carries in its payload: what the writer puts in, what the
# reader hands the host's apply, and the shapes the reader refuses instead of half-applying.

import pytest

from base.agents.incarnation.restart_transition import (
    RESTART_TRANSITION,
    RestartTransition,
    restart_transition_of,
)


def test_a_transition_survives_its_payload() -> None:
    transition = RestartTransition(
        "codex", {"llm_model": "gpt-6.1-sol", "reasoning_effort": "high"}
    )

    assert restart_transition_of(transition.payload()) == transition


def test_a_command_without_a_transition_has_none() -> None:
    assert restart_transition_of(None) is None
    assert restart_transition_of({}) is None
    assert restart_transition_of({"config_overlay": {"llm_model": "x"}}) is None


@pytest.mark.parametrize(
    "raw",
    [
        "codex",
        {"config_overlay": {}},
        {"target_harness": "", "config_overlay": {}},
        {"target_harness": "codex"},
        {"target_harness": "codex", "config_overlay": {"harness": "meiva"}},
        {"target_harness": "codex", "config_overlay": ["llm_model"]},
    ],
)
def test_a_malformed_transition_is_refused(raw: object) -> None:
    with pytest.raises(TypeError, match=RESTART_TRANSITION):
        restart_transition_of({RESTART_TRANSITION: raw})

# meiva:note
# `meiva/self/restart_plans.py`: where a restart goes (now, on the native harness; recorded for the
# turn boundary on any other, or when the overlay names another harness) and what it tells the
# caller. Asserted by value; the entry against a real database is `test_self_lifecycle.py`.
# meiva:link meiva/self/README.md

from __future__ import annotations

import pytest

from base.api_contracts.agent_contract import Harness
from meiva.self.restart_plans import (
    RestartRoute,
    deferred_restart_receipt,
    restart_refusal,
    restart_route,
    stored_native_restart_refusal,
)


def test_a_native_agent_that_stays_native_restarts_now() -> None:
    assert restart_route(current_harness="meiva", overlay=None) == RestartRoute(
        deferred=False, target=None, overlay=None
    )
    overlay: dict[str, object] = {"llm_model": "deepseek-flash"}
    assert restart_route(current_harness="meiva", overlay=overlay) == RestartRoute(
        deferred=False, target=None, overlay=overlay
    )


def test_a_native_overlay_that_repeats_the_native_harness_drops_the_key_and_restarts_now() -> None:
    assert restart_route(
        current_harness="meiva", overlay={"harness": "meiva", "llm_model": "deepseek-flash"}
    ) == RestartRoute(deferred=False, target=Harness.MEIVA, overlay={"llm_model": "deepseek-flash"})
    assert restart_route(current_harness="meiva", overlay={"harness": "meiva"}) == RestartRoute(
        deferred=False, target=Harness.MEIVA, overlay=None
    )


@pytest.mark.parametrize("target", ["claude_code", "codex", "antigravity"])
def test_a_native_agent_asking_for_an_external_harness_waits_for_the_turn_boundary(
    target: str,
) -> None:
    overlay: dict[str, object] = {"harness": target, "llm_model": "some-model"}

    assert restart_route(current_harness="meiva", overlay=overlay) == RestartRoute(
        deferred=True, target=Harness(target), overlay=overlay
    )


@pytest.mark.parametrize("current", ["claude_code", "codex", "antigravity"])
@pytest.mark.parametrize(
    "overlay", [None, {"exec_timeout_seconds": 30}, {"harness": "meiva", "llm_model": "x"}]
)
def test_an_external_agent_always_waits_for_the_turn_boundary(
    current: str, overlay: dict[str, object] | None
) -> None:
    route = restart_route(current_harness=current, overlay=overlay)

    assert route.deferred
    assert route.overlay == overlay


@pytest.mark.parametrize("bad", ["claude-code", "gpt", "", None, 7])
def test_a_name_that_is_not_a_harness_is_refused_before_anything_is_written(bad: object) -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        restart_route(current_harness="meiva", overlay={"harness": bad})


def test_a_controller_that_registered_no_harness_cannot_restart_the_agent() -> None:
    with pytest.raises(RuntimeError, match="attached controller"):
        restart_route(current_harness="external", overlay=None)


def test_the_receipt_says_nothing_happened_yet_and_what_to_do_next() -> None:
    receipt = deferred_restart_receipt(restart_route(current_harness="codex", overlay=None))

    assert "applied after the current turn ends, not now" in receipt
    assert "end the turn" in receipt
    assert "harness becomes" not in receipt
    switched = deferred_restart_receipt(
        restart_route(current_harness="codex", overlay={"harness": "meiva", "llm_model": "x"})
    )
    assert "your harness becomes 'meiva'" in switched


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ('{"detail": "harness is not launchable"}', "harness is not launchable"),
        ('{"detail": [{"msg": "bad"}]}', '{"detail": [{"msg": "bad"}]}'),
        ("plain text", "plain text"),
        ("", "no reason given"),
    ],
)
def test_a_refusal_always_names_the_status_and_the_reason_when_there_is_one(
    body: str, shown: str
) -> None:
    assert restart_refusal(409, body) == f"the restart was refused (409): {shown}"


def test_a_native_process_refuses_when_the_agent_is_stored_on_another_harness() -> None:
    assert stored_native_restart_refusal(None) is None
    assert stored_native_restart_refusal(("meiva",)) is None
    refused = stored_native_restart_refusal(("codex",))
    assert refused is not None and "'codex'" in refused

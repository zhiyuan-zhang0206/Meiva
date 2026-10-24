# meiva:note
# meiva.sdk_surface.agent_identity — the identity read off the bound context, and the owns_loop
# guard.
#
# The guard is the safety behavior the launched-script work added: a background script
# (owns_loop=False) must not drive the agent's turn loop, so the lifecycle self-actions refuse
# there. These tests lock all four guard sites + the flag logic so a future edit that drops a
# guard call or inverts the condition (which would silently reopen the watcher-compacts-its-own-
# agent bug) fails loudly.
#
# The identity a process establishes from its launch environment (`MEIVA_AGENT_ID`) is read in
# fresh interpreters whose environment the test sets (`probe_runner`); everything else binds a
# real context or hands an explicit one to the call.


import json
from typing import Any

import httpx
import pytest

import meiva
from base.agents.context import MeivaContext
from meiva.gateway_client import use_client
from meiva.sdk_surface import agent_identity
from meiva.tests.bootstrap.probe_runner import run_probe
from meiva.tests.fixtures.pin_agent import pin_agent


def _owns_loop() -> bool:
    """Whether the bound context's identity owns the turn loop."""
    context = meiva.context
    assert context.identity is not None
    return context.identity.owns_loop


def _probe(scenario: str, env: dict[str, str]) -> dict[str, Any]:
    result = run_probe(scenario, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_bound_context_sets_identity_and_flag() -> None:
    pin_agent(99, owns_loop=False)
    assert meiva.self.AGENT_ID == 99
    assert _owns_loop() is False

    pin_agent(7, owns_loop=True)
    assert meiva.self.AGENT_ID == 7
    assert _owns_loop() is True


def test_assert_self_action_permits_bootstrapped_agent() -> None:
    pin_agent(1, owns_loop=True)
    agent_identity.assert_self_action("compact")  # no raise


def test_assert_self_action_refuses_when_not_loop_owner() -> None:
    pin_agent(1, owns_loop=False)
    with pytest.raises(RuntimeError, match="can only be called from inside an agent process"):
        agent_identity.assert_self_action("compact")


def test_assert_self_action_refuses_when_identity_unset() -> None:
    # meiva:note
    # A context with no agent id — an ad-hoc `python -c "import meiva"` that never bootstrapped.
    # Must fail fast here, not defer to a null-agent_id INSERT hitting the DB NOT NULL
    # constraint.
    pin_agent(None)
    with pytest.raises(RuntimeError, match="established agent identity"):
        agent_identity.assert_self_action("terminate")


def test_require_agent_id_returns_when_set() -> None:
    pin_agent(42, owns_loop=True)
    assert agent_identity.require_agent_id() == 42


def test_require_agent_id_raises_when_none() -> None:
    with pytest.raises(RuntimeError, match="no established agent identity"):
        agent_identity.require_agent_id(MeivaContext())


def test_the_launch_environment_establishes_a_non_owning_identity() -> None:
    # meiva:note
    # A watcher / persistent-shell / schedule child: MEIVA_AGENT_ID forwarded in the env, no
    # explicit establish. Reading the identity establishes it from the env as a non-owner and
    # is_launched_child reports True — the signal that gates the lazy plugin-namespace load in
    # meiva.__getattr__. require_actor derives `agent:<id>` from the same context.
    outcome = _probe("launched_identity", {"MEIVA_AGENT_ID": "42"})
    assert (outcome["agent_id"], outcome["owns_loop"], outcome["launched_child"]) == (
        42,
        False,
        True,
    )
    assert outcome["actor"] == "agent:42"
    # meiva:note
    # Turn metadata handed in explicitly never grants a launched script the turn loop.
    assert outcome["turn_agent_id"] == 5
    assert "background script" in outcome["self_action"]


# meiva:note
# gateway / cli / ad-hoc `python -c`: no MEIVA_AGENT_ID, no identity -> not a child, so
# meiva.__getattr__ keeps fail-fast on unknown meiva.X, and `meiva.context` does not exist (the
# way `meiva.state` does not exist outside an exec turn).
def test_a_process_nobody_launched_has_no_identity_and_no_context() -> None:
    outcome = _probe("unestablished_identity", {})
    assert outcome["agent_id"] is None
    assert "no established agent identity" in outcome["require_agent_id"]
    assert "no established actor or agent identity" in outcome["require_actor"]
    assert "established agent identity" in outcome["assert_self_action"]
    assert "meiva.context requires an execution child" in outcome["context"]
    assert outcome["launched_child"] is False
    assert outcome["has_context"] is False


def test_the_agent_process_is_never_a_launched_child() -> None:
    # meiva:note
    # The real agent process owns the turn loop — it is never a "launched child", so it never
    # lazily reloads plugins (which would clear its built-in hooks).
    pin_agent(7, owns_loop=True)
    assert agent_identity.is_launched_child() is False


def test_actor_sets_system_principal() -> None:
    pin_agent(None, actor="schedule:7")
    assert agent_identity.require_actor() == "schedule:7"


def test_require_actor_derives_agent_when_no_actor() -> None:
    pin_agent(42, owns_loop=True)  # no actor
    assert agent_identity.require_actor() == "agent:42"


def test_actor_takes_precedence_over_agent_id() -> None:
    pin_agent(5, owns_loop=True, actor="schedule:9")
    assert agent_identity.require_actor() == "schedule:9"


def test_require_actor_raises_with_no_identity() -> None:
    pin_agent(None)
    with pytest.raises(RuntimeError, match="no established actor or agent identity"):
        agent_identity.require_actor()


@pytest.mark.parametrize("action", ["terminate", "restart", "resurrect"])
def test_lifecycle_default_source_requires_identity_before_http(action: str) -> None:
    pin_agent(None)

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing provenance cannot reach HTTP")

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(forbidden)) as http,
        use_client(http),
        pytest.raises(RuntimeError, match="no established actor or agent identity"),
    ):
        if action == "terminate":
            meiva.agents.terminate(7)
        elif action == "restart":
            meiva.agents.restart(7)
        else:
            meiva.agents.resurrect(7, "continue")


@pytest.mark.parametrize("action", ["compact", "restart", "terminate"])
def test_lifecycle_self_actions_refuse_from_launched_script(db_url: str, action: str) -> None:
    pin_agent(1, owns_loop=False)

    fn = getattr(meiva.self, action)
    with pytest.raises(RuntimeError, match="can only be called from inside an agent process"):
        fn("summary") if action == "compact" else fn()


def test_context_exposes_the_bound_identity() -> None:
    pin_agent(31, owns_loop=False, actor="schedule:3")
    assert meiva.context.identity is not None
    assert (meiva.context.identity.agent_id, meiva.context.identity.owns_loop) == (31, False)
    assert meiva.context.identity.actor == "schedule:3"


def test_launched_script_context_comes_from_the_environment() -> None:
    outcome = _probe("launched_identity", {"MEIVA_AGENT_ID": "58"})
    assert (outcome["agent_id"], outcome["owns_loop"]) == (58, False)

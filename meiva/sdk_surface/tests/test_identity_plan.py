# meiva:note
# The strict decision modules under `meiva/sdk_surface/pure`, as tables over values.


from __future__ import annotations

import pytest

from meiva.sdk_surface.pure.caller.attachability import (
    BoundIdentity,
    ProcessFacts,
    attach_refusal,
    refuse_unless_attachable,
)
from meiva.sdk_surface.pure.caller.identity_plan import (
    ActorFacts,
    CallerFacts,
    actor_after_external,
    actor_before_external,
    attributed_agent_id,
    self_action_refusal,
)
from meiva.sdk_surface.pure.caller.usage_plan import (
    actor_id_refusal,
    lease_refusal,
    usage_identity,
)
from meiva.sdk_surface.pure.load_plan import (
    IdentityFacts,
    LaunchFacts,
    LoadFacts,
    LoadPlan,
    LoadStep,
    MissingNameFacts,
    MissingNameStep,
    launched_process,
    plan_load,
    plan_missing_name,
)


def _caller(
    agent_id: int | None = 7,
    *,
    actor: str | None = None,
    owns_loop: bool = True,
    has_lease: bool = False,
) -> CallerFacts:
    return CallerFacts(agent_id=agent_id, actor=actor, owns_loop=owns_loop, has_lease=has_lease)


# meiva:note
# --- who is acting ---


def test_a_borrowed_identity_takes_precedence_over_the_local_agent_id() -> None:
    assert attributed_agent_id(99, _caller(42)) == 99
    assert attributed_agent_id(None, _caller(42)) == 42
    assert attributed_agent_id(None, None) is None


@pytest.mark.parametrize(
    ("borrowed", "supplied", "identity", "expected"),
    [
        (99, False, _caller(42), "agent:99"),
        (None, True, _caller(42, actor="schedule:9"), "schedule:9"),
        (None, True, _caller(42), "agent:42"),
        (None, True, _caller(None), None),
        (None, False, _caller(42, actor="schedule:9"), None),
        (None, True, None, None),
    ],
)
def test_the_actor_before_the_external_profile(
    borrowed: int | None, supplied: bool, identity: CallerFacts | None, expected: str | None
) -> None:
    facts = ActorFacts(borrowed_agent_id=borrowed, context_supplied=supplied, identity=identity)
    assert actor_before_external(facts) == expected


@pytest.mark.parametrize(
    ("supplied", "identity", "external", "actor", "recheck"),
    [
        (False, _caller(42), "external_agent:codex", "external_agent:codex", False),
        (False, _caller(42, actor="schedule:7"), None, "schedule:7", False),
        (False, _caller(42), None, "agent:42", False),
        (False, _caller(None), None, None, False),
        (False, None, None, None, True),
        (True, None, None, None, False),
    ],
)
def test_the_actor_after_the_external_profile(
    supplied: bool,
    identity: CallerFacts | None,
    external: str | None,
    actor: str | None,
    recheck: bool,
) -> None:
    decision = actor_after_external(
        ActorFacts(borrowed_agent_id=None, context_supplied=supplied, identity=identity), external
    )
    assert (decision.actor, decision.recheck_load_failure) == (actor, recheck)


def test_self_actions_need_a_loop_owner_with_an_agent_id() -> None:
    assert self_action_refusal(_caller(1), "compact") is None
    assert "background script" in (
        self_action_refusal(_caller(1, owns_loop=False), "compact") or ""
    )
    assert "established agent identity" in (self_action_refusal(_caller(None), "terminate") or "")
    assert "established agent identity" in (self_action_refusal(None, "terminate") or "")


def test_a_borrowed_lease_is_refused_before_the_actor_id_is_checked() -> None:
    assert lease_refusal(_caller(has_lease=True)) is not None
    assert lease_refusal(_caller()) is None
    assert actor_id_refusal(5) is None
    assert actor_id_refusal(True) == (TypeError, "actor agent id must be an integer")
    assert actor_id_refusal(0) == (ValueError, "actor agent id must be positive")


@pytest.mark.parametrize(
    ("borrowed", "own", "external", "expected"),
    [
        (None, None, None, {"agent_id": None, "source": "system"}),
        (None, _caller(42), None, {"agent_id": 42, "source": "agent:42"}),
        (99, _caller(42), "external_agent:codex", {"agent_id": 99, "source": "agent:99"}),
        (None, _caller(None, actor="schedule:7"), None, {"agent_id": None, "source": "schedule:7"}),
        (
            None,
            _caller(42),
            "external_agent:codex",
            {"agent_id": 42, "source": "external_agent:codex"},
        ),
    ],
)
def test_the_identity_an_sdk_usage_event_carries(
    borrowed: int | None, own: CallerFacts | None, external: str | None, expected: dict[str, object]
) -> None:
    assert usage_identity(borrowed, own, external) == expected


# meiva:note
# --- who may attach ---


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        (ProcessFacts(True, False, None), "a native agent runtime cannot attach a harness"),
        (
            ProcessFacts(False, False, BoundIdentity(True, 1, False)),
            "this process already has an external attachment",
        ),
        (
            ProcessFacts(False, False, BoundIdentity(False, 1, True)),
            "a native agent runtime cannot attach a harness",
        ),
        (ProcessFacts(False, True, None), "an exec turn cannot attach a harness"),
        (ProcessFacts(False, False, BoundIdentity(False, None, True)), None),
        (ProcessFacts(False, False, None), None),
    ],
)
def test_only_an_ordinary_process_may_borrow_an_identity(
    facts: ProcessFacts, message: str | None
) -> None:
    assert attach_refusal(facts, subject="a harness") == message
    if message is None:
        refuse_unless_attachable(facts, subject="a harness")
    else:
        with pytest.raises(RuntimeError, match=message):
            refuse_unless_attachable(facts, subject="a harness")


# meiva:note
# --- what a plugin-load request does ---


def _load(**changes: bool) -> LoadFacts:
    facts = {
        "surface": True,
        "installed": False,
        "faces": False,
        "attempted": False,
        "failed": False,
        "config_supplied": False,
        "context_bound": False,
        "launched": False,
    }
    return LoadFacts(**{**facts, **changes})


@pytest.mark.parametrize(
    ("facts", "plan"),
    [
        (_load(failed=True, attempted=True, installed=True), LoadPlan(LoadStep.RAISE_FAILURE)),
        (_load(installed=True), LoadPlan(LoadStep.BIND_CATALOG)),
        (_load(installed=True, surface=False, faces=True), LoadPlan(LoadStep.BIND_CATALOG)),
        (_load(installed=True, surface=False, attempted=True), LoadPlan(LoadStep.NOTHING)),
        (_load(installed=True, surface=False), LoadPlan(LoadStep.UPGRADE_FACES)),
        (_load(attempted=True), LoadPlan(LoadStep.NOTHING)),
        (_load(), LoadPlan(LoadStep.LOAD_SURFACE)),
        (_load(launched=True), LoadPlan(LoadStep.LOAD_SURFACE, prepare_launched=True)),
        (_load(launched=True, config_supplied=True), LoadPlan(LoadStep.LOAD_SURFACE)),
        (_load(launched=True, context_bound=True), LoadPlan(LoadStep.LOAD_SURFACE)),
    ],
)
def test_the_load_step_follows_the_slot_and_the_request(facts: LoadFacts, plan: LoadPlan) -> None:
    assert plan_load(facts) == plan


@pytest.mark.parametrize(
    ("facts", "launched"),
    [
        (LaunchFacts(True, False, None, True), False),
        (LaunchFacts(False, True, IdentityFacts(True, False), False), True),
        (LaunchFacts(False, True, IdentityFacts(True, True), True), False),
        (LaunchFacts(False, True, IdentityFacts(False, False), True), False),
        (LaunchFacts(False, True, None, True), False),
        (LaunchFacts(False, False, None, True), True),
        (LaunchFacts(False, False, None, False), False),
    ],
)
def test_a_process_is_launched_by_its_bound_identity_or_else_by_the_environment(
    facts: LaunchFacts, launched: bool
) -> None:
    assert launched_process(facts) is launched


# meiva:note
# The suppressed diagnostics are deliberate here: the code under test is handed the argument
# shapes the type checker rejects.
def _missing(**changes: bool | None) -> MissingNameFacts:
    facts: dict[str, bool | None] = {
        "private": False,
        "entry_complete": True,
        "failed": False,
        "installed": False,
        "attempted": False,
        "launched_child": None,
    }
    return MissingNameFacts(**{**facts, **changes})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("facts", "step"),
    [
        (_missing(private=True), MissingNameStep.IGNORE),
        (_missing(entry_complete=False), MissingNameStep.IGNORE),
        (_missing(failed=True), MissingNameStep.RAISE_FAILURE),
        (_missing(installed=True), MissingNameStep.IGNORE),
        (_missing(attempted=True), MissingNameStep.IGNORE),
        (_missing(), MissingNameStep.ASK_LAUNCHED),
        (_missing(launched_child=False), MissingNameStep.IGNORE),
        (_missing(launched_child=True), MissingNameStep.LOAD),
        (_missing(launched_child=True, installed=True), MissingNameStep.LOADED),
    ],
)
def test_a_missing_public_name_loads_plugins_only_for_a_launched_child(
    facts: MissingNameFacts, step: MissingNameStep
) -> None:
    assert plan_missing_name(facts) is step

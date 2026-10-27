# meiva:note
# `meiva/sdk_surface/pure/plans/metering/meter_plan.py` unit tests: how the namespace walk tells
# members apart, the name an MCP call is metered under and the usage identity a call is stamped
# with (unbound, owner, borrowed lease, external profile). Asserted by value; the installed
# recorders are covered by `meiva/tests/namespaces/sdk_surface/test_metering.py`.

from __future__ import annotations

from types import ModuleType, SimpleNamespace

from base.agents.context.identity import AgentIdentity, ExternalLease
from base.api_contracts.caller_identity import CallerIdentity
from meiva.sdk_surface.pure.plans.metering.meter_plan import (
    MemberKind,
    call_identity,
    mcp_call_name,
    member_kind,
)

_EXTERNAL = CallerIdentity(kind="external_agent", subject="codex", instance="a1")
_LEASE = ExternalLease(agent_id=321, validate=lambda: 321, config=lambda: None)


def _routine() -> None: ...


def test_the_walk_wraps_routines_descends_into_namespaces_and_skips_the_rest() -> None:
    assert member_kind(_routine) is MemberKind.ROUTINE
    assert member_kind(len) is MemberKind.ROUTINE
    assert member_kind(ModuleType("m")) is MemberKind.NAMESPACE
    assert member_kind(SimpleNamespace()) is MemberKind.NAMESPACE
    assert member_kind(SimpleNamespace) is MemberKind.OTHER
    assert member_kind(3) is MemberKind.OTHER
    assert member_kind(None) is MemberKind.OTHER


def test_an_mcp_call_is_metered_under_its_runtime_server_and_tool() -> None:
    assert mcp_call_name("exa", "search") == "mcps.exa.search"


def test_an_unbound_caller_is_a_system_caller() -> None:
    assert call_identity(None, None) == {"agent_id": None, "source": "system"}


def test_an_unbound_caller_under_an_external_profile_is_stamped_with_its_source() -> None:
    assert call_identity(None, _EXTERNAL) == {
        "agent_id": None,
        "source": "external_agent:codex:a1",
    }


def test_an_agent_process_is_stamped_with_its_agent() -> None:
    own = AgentIdentity(agent_id=7, owns_loop=True, actor="agent:7")
    assert call_identity(own, None) == {"agent_id": 7, "source": "agent:7"}


def test_a_process_with_a_principal_but_no_agent_is_stamped_with_the_principal() -> None:
    own = AgentIdentity(agent_id=None, owns_loop=False, actor="schedule:4")
    assert call_identity(own, None) == {"agent_id": None, "source": "schedule:4"}


def test_a_borrowed_lease_attributes_the_call_to_the_leased_agent_over_the_owner() -> None:
    own = AgentIdentity(agent_id=7, owns_loop=False, lease=_LEASE)
    assert call_identity(own, None) == {"agent_id": 321, "source": "agent:321"}


def test_an_external_profile_does_not_override_a_borrowed_lease() -> None:
    own = AgentIdentity(agent_id=None, owns_loop=False, lease=_LEASE)
    assert call_identity(own, _EXTERNAL) == {"agent_id": 321, "source": "agent:321"}


def test_an_external_profile_names_a_process_that_has_no_agent_of_its_own() -> None:
    own = AgentIdentity(agent_id=None, owns_loop=False)
    assert call_identity(own, _EXTERNAL) == {
        "agent_id": None,
        "source": "external_agent:codex:a1",
    }

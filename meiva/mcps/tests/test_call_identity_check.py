from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from base.agents.context.identity import AgentIdentity, ExternalLease
from meiva.mcps._tools import call_raw, list_tools, resolve_agent_id
from meiva.mcps.tests._mcps_helpers import local_mcp_clients, tool_source
from meiva.mcps.tests.stub_daemon import Respond, StubDaemon, ok_line
from meiva.mcps.tests.stub_daemon import stub_daemon as stub_daemon

Start = Callable[[Respond], StubDaemon]


# meiva:note
# A borrowed identity whose lease answers `validate` with the agent id it still permits.
def borrowed(validate: Callable[[], int], *, agent_id: int = 7) -> AgentIdentity:
    lease = ExternalLease(agent_id=agent_id, validate=validate, config=lambda: None)
    return AgentIdentity(agent_id=agent_id, owns_loop=False, lease=lease)


def test_no_identity_has_no_agent() -> None:
    assert resolve_agent_id(None) is None


def test_an_identity_without_a_lease_is_its_own_agent_id() -> None:
    assert resolve_agent_id(AgentIdentity(agent_id=4, owns_loop=True)) == 4
    assert resolve_agent_id(AgentIdentity(agent_id=None, owns_loop=True)) is None


def test_a_valid_lease_takes_precedence_over_the_identitys_own_id() -> None:
    assert resolve_agent_id(borrowed(lambda: 9, agent_id=7)) == 9


def test_an_invalid_lease_raises_instead_of_falling_back() -> None:
    def expired() -> int:
        raise RuntimeError("external lease expired")

    with pytest.raises(RuntimeError, match="expired"):
        resolve_agent_id(borrowed(expired))


def test_a_failed_identity_check_stops_the_call_before_the_daemon(tmp_path: Path) -> None:
    def expired() -> int:
        raise RuntimeError("external lease expired")

    # meiva:note
    # No daemon listens here: a call that got past the identity check would fail to connect instead
    # of raising the lease error.
    unreachable = local_mcp_clients(tmp_path / "absent.sock")
    source = tool_source(unreachable, identity=borrowed(expired))

    with pytest.raises(RuntimeError, match="expired"):
        call_raw(source, "example", "side_effect")
    with pytest.raises(RuntimeError, match="expired"):
        list_tools(source, "example")


def test_every_request_carries_the_agent_id_the_lease_permits_now(stub_daemon: Start) -> None:
    permitted = iter([9, 10, 11])
    daemon = stub_daemon(
        lambda request: [
            ok_line(request, {"content": [], "isError": False})
            if request["method"] == "call_tool"
            else ok_line(request, [])
        ]
    )
    source = tool_source(
        local_mcp_clients(Path(daemon.path)), identity=borrowed(lambda: next(permitted))
    )

    call_raw(source, "example", "side_effect", a=1)
    list_tools(source, "example")
    call_raw(source, "example", "side_effect")

    assert [request["agent_id"] for request in daemon.requests] == [9, 10, 11]


def test_a_process_without_an_identity_sends_no_agent_id(stub_daemon: Start) -> None:
    daemon = stub_daemon(lambda request: [ok_line(request, {"content": [], "isError": False})])
    call_raw(tool_source(local_mcp_clients(Path(daemon.path))), "example", "side_effect")
    assert daemon.requests[0]["agent_id"] is None

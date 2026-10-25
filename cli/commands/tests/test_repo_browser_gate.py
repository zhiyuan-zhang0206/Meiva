# meiva:note
# The browser and computer-mcp daemons belong to the agent-runner capability.
#
# Whether the settings flags or the host keep them off the start roster, and the reason the
# diagnostic view shows, is decided by `services.supervision.roster.gate_plan` and tested there
# over values; this file owns only which capability the roster scopes them to. The diagnostic view
# keeps a gated-out service WITH its reason, so membership in it does not depend on the flags or
# on the host (here a scripted one with no display).


from base.cluster.machine import MachineIdentityReader
from base.host.system.tests.fake_commands import fake_host
from cli.database.tests.command_inputs import command_machine as command_machine
from services.supervision.roster import spec


def _annotated_sessions(role: str, *, machine: MachineIdentityReader) -> set[str]:
    return {
        s.session
        for s, _reason in spec.services_for_capabilities_annotated(
            frozenset({role}), machine=machine, host=fake_host()
        )
    }


def test_browser_is_an_agent_runner_service(command_machine: MachineIdentityReader) -> None:
    assert "browser" in _annotated_sessions("agent-runner", machine=command_machine)


def test_browser_is_never_a_gateway_service(command_machine: MachineIdentityReader) -> None:
    assert "browser" not in _annotated_sessions("gateway", machine=command_machine)


def test_computer_mcp_is_an_agent_runner_service(command_machine: MachineIdentityReader) -> None:
    assert "computer-mcp" in _annotated_sessions("agent-runner", machine=command_machine)


def test_computer_mcp_is_never_a_gateway_service(command_machine: MachineIdentityReader) -> None:
    assert "computer-mcp" not in _annotated_sessions("gateway", machine=command_machine)

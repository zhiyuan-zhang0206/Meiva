# meiva:note
# The heartbeat belongs to the gateway capability, whatever its settings flag says.
#
# Whether the flag keeps it off the start roster, and the reason the diagnostic view shows, is
# decided by `services.supervision.roster.gate_plan` and tested there over values; this file owns
# only which capability the roster scopes the heartbeat to.


from base.cluster.machine import MachineIdentity
from base.host.system.tests.fake_commands import fake_host
from cli.database.tests.command_inputs import command_machine as command_machine
from services.supervision.roster import spec


def _annotated_sessions(role: str, *, machine: MachineIdentity) -> set[str]:
    return {
        s.session
        for s, _reason in spec.services_for_capabilities_annotated(
            frozenset({role}), machine=machine, host=fake_host()
        )
    }


def test_heartbeat_is_a_gateway_service(command_machine: MachineIdentity) -> None:
    assert "heartbeat" in _annotated_sessions("gateway", machine=command_machine)


def test_heartbeat_is_never_an_agent_runner_service(command_machine: MachineIdentity) -> None:
    assert "heartbeat" not in _annotated_sessions("agent-runner", machine=command_machine)

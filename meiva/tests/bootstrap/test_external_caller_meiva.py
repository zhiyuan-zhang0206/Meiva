# meiva:note
# The external tool profile (`MEIVA_CALLER_IDENTITY`) and the actor a call is stamped with.
#
# The decision is the pure `actor_before_external` / `actor_after_external`; the profile comes
# from the environment of the process the call runs in, so the facade is read in a real child.


import json

from meiva.sdk_surface.pure.caller.identity_plan import (
    ActorFacts,
    CallerFacts,
    actor_after_external,
    actor_before_external,
)
from meiva.tests.bootstrap.probe_runner import run_probe

_PROFILE = '{"kind":"external_agent","subject":"codex"}'
_CODEX = "external_agent:codex"


def _local(agent_id: int) -> CallerFacts:
    return CallerFacts(agent_id=agent_id, actor=None, owns_loop=True, has_lease=False)


def test_external_profile_overrides_the_inherited_local_identity() -> None:
    facts = ActorFacts(borrowed_agent_id=None, context_supplied=False, identity=_local(405))

    assert actor_before_external(facts) is None
    assert actor_after_external(facts, _CODEX).actor == _CODEX


def test_explicit_host_context_remains_authoritative() -> None:
    for agent_id in (405, 406):
        facts = ActorFacts(borrowed_agent_id=None, context_supplied=True, identity=_local(agent_id))
        assert actor_before_external(facts) == f"agent:{agent_id}"


def test_the_facade_reads_the_profile_from_its_process_environment() -> None:
    result = run_probe("external_profile_actor", env={"MEIVA_CALLER_IDENTITY": _PROFILE})

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "local": _CODEX,
        "explicit": "agent:406",
    }


# meiva:note
# A profile the SDK cannot parse is a fault in who is calling. The plugin load that reads the
# process environment refuses it and leaves no installation, so no SDK call runs under a guessed
# identity (before the profile was an installation value, each call's admission refused it).
def test_an_unreadable_profile_fails_the_plugin_load_and_leaves_no_installation() -> None:
    result = run_probe("unreadable_external_profile", env={"MEIVA_CALLER_IDENTITY": "{not json"})

    assert result.returncode == 0, result.stderr
    outcome, error_type = result.stdout.strip().splitlines()[-2:]
    assert json.loads(outcome) == {"rejected": True, "installed": False}
    assert error_type == "ValidationError"

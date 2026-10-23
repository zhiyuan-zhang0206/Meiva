import pytest

from agent.takeover.gate import LeasePhase
from agent.takeover.relay import (
    ChildView,
    LeaseView,
    Poll,
    RelayBinding,
    RelayRoute,
    RelayWait,
    Supervision,
    endpoint_unusable_reason,
    relay_command,
    relay_poll,
    relay_route,
    startup_failure,
    supervision_step,
)

SESSION_PROVIDERS = ("claude", "dsh")
CREDENTIAL = "private-test-credential"
WAIT = RelayWait(timeout_s=30.0, poll_s=1.0)


@pytest.mark.parametrize(
    ("provider", "route"),
    [
        ("claude", RelayRoute.SESSION),
        ("dsh", RelayRoute.SESSION),
        ("codex", RelayRoute.SPAWNED),
        ("antigravity", RelayRoute.SPAWNED),
        (None, RelayRoute.UNBOUND),
        ("unknown", RelayRoute.UNBOUND),
    ],
)
def test_the_provider_decides_who_brings_the_relay_up(
    provider: str | None, route: RelayRoute
) -> None:
    assert relay_route(provider, session_providers=SESSION_PROVIDERS) is route


@pytest.mark.parametrize(
    ("phase", "fresh", "waited_s", "poll"),
    [
        (LeasePhase.ACCEPTED, True, 0.0, Poll.READY),
        (LeasePhase.ACCEPTED, True, 99.0, Poll.READY),
        (LeasePhase.ACCEPTED, False, 0.0, Poll.WAIT),
        (LeasePhase.ACCEPTED, False, 29.9, Poll.WAIT),
        (LeasePhase.ACCEPTED, False, 30.0, Poll.GIVE_UP),
        (LeasePhase.ACTIVE, True, 0.0, Poll.ABANDON),
        (LeasePhase.ENDED, True, 0.0, Poll.ABANDON),
        (LeasePhase.REQUESTED, False, 0.0, Poll.ABANDON),
        (None, True, 0.0, Poll.ABANDON),
    ],
)
def test_the_activation_wait_ends_on_a_fresh_heartbeat_or_a_spent_wait(
    phase: LeasePhase | None, fresh: bool, waited_s: float, poll: Poll
) -> None:
    assert relay_poll(phase, heartbeat_fresh=fresh, waited_s=waited_s, wait=WAIT) is poll


def test_a_running_child_has_no_startup_failure_and_an_exited_one_names_its_exit_code() -> None:
    assert startup_failure("codex", None) is None
    assert startup_failure("codex", 5) == (
        "codex relay exited during startup (exit code 5); its transport error is on stderr"
    )
    assert "exit code 0" in str(startup_failure("antigravity", 0))


def test_an_unusable_antigravity_endpoint_is_named_in_the_rollback_reason() -> None:
    assert endpoint_unusable_reason("record /x is not json") == (
        "the Antigravity endpoint is unusable: record /x is not json"
    )


def _child(
    *, lease: str = "L", generation: int = 1, exited: bool = False, age_s: float = 600.0
) -> ChildView:
    return ChildView(lease_id=lease, generation=generation, exited=exited, age_s=age_s)


def _lease(
    phase: LeasePhase = LeasePhase.ACTIVE,
    *,
    fresh: bool = False,
    minted_age_s: float | None = None,
    generation: int = 1,
) -> LeaseView:
    return LeaseView(
        lease_id="L",
        generation=generation,
        phase=phase,
        heartbeat_fresh=fresh,
        minted_age_s=minted_age_s,
    )


def _step(
    child: ChildView | None, lease: LeaseView | None, *, native_held: bool = True
) -> Supervision:
    return supervision_step(child, lease, native_held=native_held, wait=WAIT)


@pytest.mark.parametrize(
    "child",
    [_child(lease="L2"), _child(generation=2), _child(lease="L2", generation=2, exited=True)],
    ids=["other-lease", "other-generation", "other-lease-exited"],
)
def test_a_delayed_snapshot_of_another_relay_never_touches_this_hosts_child(
    child: ChildView,
) -> None:
    assert _step(child, _lease()) is Supervision.IDLE
    assert _step(child, _lease(LeasePhase.ENDED)) is Supervision.IDLE


@pytest.mark.parametrize("lease", [None, _lease(LeasePhase.ENDED)], ids=["no-lease", "ended"])
def test_an_absent_or_ended_lease_retires_the_child_only_after_the_database_confirms(
    lease: LeaseView | None,
) -> None:
    assert _step(_child(), lease) is Supervision.RETIRE_IF_ENDED
    assert _step(None, lease) is Supervision.IDLE


@pytest.mark.parametrize("phase", [LeasePhase.REQUESTED, LeasePhase.ACCEPTED])
def test_only_an_active_lease_is_supervised(phase: LeasePhase) -> None:
    assert _step(_child(exited=True), _lease(phase)) is Supervision.IDLE
    assert _step(None, _lease(phase)) is Supervision.IDLE


def test_a_live_relay_with_a_fresh_heartbeat_needs_nothing() -> None:
    assert _step(_child(), _lease(fresh=True)) is Supervision.IDLE
    assert _step(None, _lease(fresh=True)) is Supervision.IDLE


@pytest.mark.parametrize(
    ("fresh", "age_s"), [(True, 600.0), (False, 600.0), (True, 0.0), (False, 0.0)]
)
def test_a_confirmed_child_exit_ends_the_lease_whatever_the_heartbeat_and_startup_window(
    fresh: bool, age_s: float
) -> None:
    child = _child(exited=True, age_s=age_s)
    assert _step(child, _lease(fresh=fresh, minted_age_s=0.0)) is Supervision.END_DEAD


def test_a_host_without_the_native_incarnation_neither_ends_a_lease_nor_records_degradation() -> (
    None
):
    assert _step(_child(exited=True), _lease(), native_held=False) is Supervision.IDLE
    assert _step(None, _lease(), native_held=False) is Supervision.IDLE


@pytest.mark.parametrize(
    ("child", "minted_age_s", "step"),
    [
        (None, None, Supervision.RECORD_STALE),
        (None, 3600.0, Supervision.RECORD_STALE),
        (None, 5.0, Supervision.IDLE),
        (_child(age_s=600.0), None, Supervision.RECORD_STALE),
        (_child(age_s=5.0), None, Supervision.IDLE),
        (_child(age_s=600.0), 5.0, Supervision.IDLE),
    ],
    ids=[
        "no-child-never-minted",
        "no-child-old-credential",
        "no-child-fresh-credential",
        "old-child",
        "fresh-child",
        "old-child-fresh-credential",
    ],
)
def test_a_stale_relay_is_recorded_as_degraded_outside_its_startup_window(
    child: ChildView | None, minted_age_s: float | None, step: Supervision
) -> None:
    assert _step(child, _lease(minted_age_s=minted_age_s)) is step


def _binding(
    *,
    provider: str = "codex",
    codex_remote: str | None = None,
    antigravity_endpoint_file: str | None = None,
    codex_home: str | None = None,
) -> RelayBinding:
    return RelayBinding(
        agent_id=42,
        lease_id="lease-1",
        provider=provider,
        thread_id="thread-1",
        codex_remote=codex_remote,
        antigravity_endpoint_file=antigravity_endpoint_file,
        codex_home=codex_home,
    )


def test_the_relay_command_carries_lease_identifiers_and_never_the_credential() -> None:
    command = relay_command(
        ("meiva",), _binding(), token=CREDENTIAL, forwarded_env={"PATH": "/bin"}
    )
    assert command.argv == (
        "meiva",
        "impersonate",
        "relay",
        "42",
        "--lease-id",
        "lease-1",
        "--provider",
        "codex",
        "--thread-id",
        "thread-1",
        "--token-stdin",
    )
    assert command.stdin == CREDENTIAL.encode() + b"\n"
    assert all(CREDENTIAL not in part for part in command.argv)
    assert CREDENTIAL not in command.env.values()


def test_a_codex_remote_extends_the_command_and_the_antigravity_record_stays_out_of_it() -> None:
    command = relay_command(
        ("meiva",),
        _binding(codex_remote="unix:///var/run/codex.sock"),
        token=CREDENTIAL,
        forwarded_env={},
    )
    assert command.argv[-2:] == ("--codex-remote", "unix:///var/run/codex.sock")
    assert "--antigravity-endpoint-file" not in command.argv


def test_an_antigravity_command_names_the_endpoint_record_by_path() -> None:
    command = relay_command(
        ("meiva",),
        _binding(
            provider="antigravity", antigravity_endpoint_file="/var/lib/meiva/conversation.json"
        ),
        token=CREDENTIAL,
        forwarded_env={},
    )
    assert command.argv[-2:] == ("--antigravity-endpoint-file", "/var/lib/meiva/conversation.json")
    assert "--codex-remote" not in command.argv


def test_the_program_prefix_replaces_the_entry_point_and_the_lease_codex_home_joins_the_environment() -> (
    None
):
    command = relay_command(
        ("python", "-I", "stub.py"),
        _binding(codex_home="/var/lib/codex-home"),
        token=CREDENTIAL,
        forwarded_env={"PATH": "/bin", "CODEX_HOME": "/elsewhere"},
    )
    assert command.argv[:5] == ("python", "-I", "stub.py", "impersonate", "relay")
    assert dict(command.env) == {"PATH": "/bin", "CODEX_HOME": "/var/lib/codex-home"}

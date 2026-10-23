from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agent.takeover.gate import LeasePhase

SPAWNED_PROVIDERS = ("codex", "antigravity")
UNBOUND_REASON = "request has no relay binding"
SESSION_RELAY_DOWN_REASON = "the controller relay is not running (no fresh heartbeat)"
CREDENTIAL_RENEWED_REASON = "relay credential was re-provisioned"
NOT_READY_REASON = "relay did not become ready in time"
STALE_RELAY_REASON = "relay unavailable; automatic recovery unsupported"


@dataclass(frozen=True)
class RelayWait:
    timeout_s: float = 30.0
    poll_s: float = 1.0


# meiva:note
# The facts a native host reads once and hands to the takeover shells: its own machine
# name, the root of the per-agent workspaces, the command prefix that starts a bound
# relay (the `meiva` entry point; `impersonate relay ...` follows it) and the relay
# readiness tuning. A test hands in a temporary root, a small script as the program and
# a short wait.
# meiva:link agent/docs/impersonation/relay-supervision.meiva.okf.md
@dataclass(frozen=True)
class TakeoverHost:
    local_machine: str
    workspaces_root: Path
    relay_program: tuple[str, ...]
    wait: RelayWait = RelayWait()

    def workspace(self, agent_id: int) -> Path:
        return self.workspaces_root / str(agent_id)


class RelayRoute(Enum):
    SESSION = "session"
    SPAWNED = "spawned"
    UNBOUND = "unbound"


class Poll(Enum):
    READY = "ready"
    WAIT = "wait"
    ABANDON = "abandon"
    GIVE_UP = "give_up"


class Supervision(Enum):
    IDLE = "idle"
    RETIRE_IF_ENDED = "retire_if_ended"
    END_DEAD = "end_dead"
    RECORD_STALE = "record_stale"


@dataclass(frozen=True)
class ChildView:
    lease_id: str
    generation: int
    exited: bool
    age_s: float


@dataclass(frozen=True)
class LeaseView:
    lease_id: str
    generation: int
    phase: LeasePhase
    heartbeat_fresh: bool
    minted_age_s: float | None


@dataclass(frozen=True)
class RelayBinding:
    agent_id: int
    lease_id: str
    provider: str
    thread_id: str
    codex_remote: str | None
    antigravity_endpoint_file: str | None
    codex_home: str | None


@dataclass(frozen=True)
class RelayCommand:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    stdin: bytes


# meiva:note
# Who brings the bound relay up. A Claude or dsh relay runs inside the controller's own
# session and only has to heartbeat. Codex and Antigravity relays are spawned by the
# accepting host. A lease without any of those bindings cannot have been prepared.
# meiva:link agent/docs/impersonation/relay-supervision.meiva.okf.md
def relay_route(provider: str | None, *, session_providers: Collection[str]) -> RelayRoute:
    if provider in session_providers:
        return RelayRoute.SESSION
    if provider in SPAWNED_PROVIDERS:
        return RelayRoute.SPAWNED
    return RelayRoute.UNBOUND


# meiva:note
# One poll of the activation gate's wait for the first relay heartbeat. A lease that is
# no longer accepted (someone else already ended it) needs no relay. A fresh heartbeat
# ends the wait. A heartbeat that is still missing when the wait is spent fails the
# gate. `phase` is None when the latest snapshot is another lease or missing.
# meiva:link agent/docs/impersonation/relay-supervision.meiva.okf.md
def relay_poll(
    phase: LeasePhase | None, *, heartbeat_fresh: bool, waited_s: float, wait: RelayWait
) -> Poll:
    if phase is not LeasePhase.ACCEPTED:
        return Poll.ABANDON
    if heartbeat_fresh:
        return Poll.READY
    if waited_s >= wait.timeout_s:
        return Poll.GIVE_UP
    return Poll.WAIT


def startup_failure(provider: str, exit_code: int | None) -> str | None:
    if exit_code is None:
        return None
    return (
        f"{provider} relay exited during startup (exit code {exit_code});"
        " its transport error is on stderr"
    )


def endpoint_unusable_reason(detail: str) -> str:
    return f"the Antigravity endpoint is unusable: {detail}"


# meiva:note
# Whether the snapshot of the lease belongs to another lease or relay generation than this host's
# child.
def _foreign_snapshot(child: ChildView | None, lease: LeaseView | None) -> bool:
    return (
        child is not None
        and lease is not None
        and (child.lease_id, child.generation) != (lease.lease_id, lease.generation)
    )


# meiva:note
# Whether the relay is still inside its startup window: the child was just spawned or the
# credential was just minted.
def _in_startup_window(child: ChildView | None, lease: LeaseView, wait: RelayWait) -> bool:
    if child is not None and child.age_s < wait.timeout_s:
        return True
    return lease.minted_age_s is not None and lease.minted_age_s < wait.timeout_s


# meiva:note
# What one supervision pass does about the bound relay. A snapshot that belongs to
# another lease or relay generation than this host's child never retires that child.
# No lease, or an ended one, retires the child once the database confirms the end.
# Only an active lease is supervised. Confirmed exit of this host's own child ends the
# lease (nothing can push inbound messages any more). A relay that is merely stale is
# recorded as degraded and never replaced, unless it is still inside its startup window
# (the child was just spawned, or the credential was just minted). A host that does not
# hold the native incarnation can neither end a lease nor record degradation.
# meiva:link agent/docs/impersonation/relay-supervision.meiva.okf.md
def supervision_step(
    child: ChildView | None, lease: LeaseView | None, *, native_held: bool, wait: RelayWait
) -> Supervision:
    if _foreign_snapshot(child, lease):
        return Supervision.IDLE
    if lease is None or lease.phase is LeasePhase.ENDED:
        return Supervision.IDLE if child is None else Supervision.RETIRE_IF_ENDED
    if lease.phase is not LeasePhase.ACTIVE:
        return Supervision.IDLE
    exited = child is not None and child.exited
    if (not exited and lease.heartbeat_fresh) or not native_held:
        return Supervision.IDLE
    if exited:
        return Supervision.END_DEAD
    if _in_startup_window(child, lease, wait):
        return Supervision.IDLE
    return Supervision.RECORD_STALE


# meiva:note
# The process a host starts for a bound codex or Antigravity relay. The command line
# carries lease identifiers and record paths only: the scoped relay token arrives over
# the child's stdin (`--token-stdin`), and an Antigravity CSRF token never travels at
# all because the relay reads the owner-only record this path names. The environment is
# the session projection plus the lease's Codex home when it names one.
# meiva:link agent/docs/impersonation/relay-supervision.meiva.okf.md
def relay_command(
    program: tuple[str, ...],
    binding: RelayBinding,
    *,
    token: str,
    forwarded_env: Mapping[str, str],
) -> RelayCommand:
    argv = [
        *program,
        "impersonate",
        "relay",
        str(binding.agent_id),
        "--lease-id",
        binding.lease_id,
        "--provider",
        binding.provider,
        "--thread-id",
        binding.thread_id,
        "--token-stdin",
    ]
    if binding.codex_remote is not None:
        argv.extend(["--codex-remote", binding.codex_remote])
    if binding.antigravity_endpoint_file is not None:
        argv.extend(["--antigravity-endpoint-file", binding.antigravity_endpoint_file])
    env = dict(forwarded_env)
    if binding.codex_home is not None:
        env["CODEX_HOME"] = binding.codex_home
    return RelayCommand(argv=tuple(argv), env=env, stdin=token.encode() + b"\n")

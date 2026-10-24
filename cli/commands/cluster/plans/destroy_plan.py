# meiva:note
# What `meiva cluster destroy` asks, says and does, as values.
#
# The shell reads the home's recorded start intent and this host's platform, then executes
# the returned plan step by step. Destroy is irreversible, so the order and the early exits
# are the point: the terminal intent is published first (a concurrent start must refuse from
# the first moment of teardown), the home is marked detached only after the stop and every
# retirement succeeded, and the data directories go last, outside the start lock.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "CONFIRMATION_PROMPT",
    "NO_TERMINAL_REFUSAL",
    "STOP_INCOMPLETE",
    "DestroyFacts",
    "DestroyPlan",
    "DestroyStep",
    "DropData",
    "PublishIntent",
    "RetireHelper",
    "RetireJobs",
    "StopCluster",
    "answer_confirms",
    "cleanup_incomplete",
    "destroy_prompt",
    "plan_destroy",
    "scheduled_jobs_failure",
    "stop_argv",
]

CONFIRMATION_PROMPT = "Type the home path to confirm: "
STOP_INCOMPLETE = "cluster stop incomplete; destroy intent retained"

# meiva:note
# Destroy needs stdin and stdout to be terminals and offers no flag that skips the
# confirmation.
NO_TERMINAL_REFUSAL = (
    "✗ meiva cluster destroy: refusing without an interactive terminal "
    "(over ssh, use `ssh -t`); there is no flag that skips the confirmation"
)


# meiva:note
# Everything the plan depends on, read by the shell: the home destroy acts on, the
# capabilities its start intent records, the stop's mode and budget, the `meiva` of this
# checkout, the host's platform and the data directories `--drop-db` would delete.
@dataclass(frozen=True)
class DestroyFacts:
    home: str
    roles: tuple[str, ...]
    stop_mode: str
    stop_timeout_s: float
    meiva_executable: str
    platform: str
    helper_port: int
    drop_dirs: tuple[str, ...]


# meiva:note
# Write the destroy-intent marker with this state ("destroying" or "detached"), then say
# `announce` if there is something to say.
@dataclass(frozen=True)
class PublishIntent:
    state: str
    announce: str | None

    # meiva:note
    # The marker file's content.
    def marker(self) -> bytes:
        return f'{{"version":1,"state":"{self.state}"}}\n'.encode()


# meiva:note
# Run `meiva stop` as a child with this argv, from the checkout.
@dataclass(frozen=True)
class StopCluster:
    argv: tuple[str, ...]


# meiva:note
# Remove every OS-scheduled job this host registered.
@dataclass(frozen=True)
class RetireJobs: ...


# meiva:note
# Retire this home's permissions helper (macOS only: no other platform has one).
@dataclass(frozen=True)
class RetireHelper:
    helper_port: int


# meiva:note
# Delete these data directories, which cannot be undone, then say `announce`.
@dataclass(frozen=True)
class DropData:
    directories: tuple[str, ...]
    announce: str


DestroyStep = PublishIntent | StopCluster | RetireJobs | RetireHelper | DropData


# meiva:note
# `under_lock` runs while the home's start-intent lock is held; `afterwards` runs once it is
# released.
@dataclass(frozen=True)
class DestroyPlan:
    under_lock: tuple[DestroyStep, ...]
    afterwards: tuple[DestroyStep, ...]


# meiva:note
# The argv of the child `meiva stop`.
#
# Stop this cluster entirely: services, shell sessions, browser, extras, data plane. A detach
# leaves nothing of the cluster running, so every keep-or-stop choice is "stop" by the verb's
# meaning; the operator names the mode and the budget. A gateway also stops its own data
# plane: `--drop-db` then removes the data directories of a genuinely stopped instance, never
# a live one. `--yes` because the child has no terminal to confirm on (destroy confirmed
# already).
def stop_argv(
    meiva_executable: str, roles: Sequence[str], mode: str, timeout_s: float
) -> tuple[str, ...]:
    return (
        meiva_executable,
        "stop",
        "--yes",
        "--close-shell-sessions",
        "--stop-browser",
        "--stop-extras",
        "--keep-no-services",
        "--mode",
        mode,
        "--timeout",
        f"{timeout_s:g}",
        *(("--stop-infra",) if "gateway" in roles else ()),
    )


def plan_destroy(facts: DestroyFacts) -> DestroyPlan:
    stop = StopCluster(
        stop_argv(facts.meiva_executable, facts.roles, facts.stop_mode, facts.stop_timeout_s)
    )
    helper = (RetireHelper(facts.helper_port),) if facts.platform == "darwin" else ()
    dropped = f"✓ removed '{facts.home}' per-cluster data plane (pg + redis data dirs)"
    return DestroyPlan(
        under_lock=(
            PublishIntent("destroying", announce=None),
            stop,
            RetireJobs(),
            *helper,
            PublishIntent("detached", announce=f"detached {facts.home} after exact cleanup"),
        ),
        afterwards=(DropData(facts.drop_dirs, announce=dropped),) if facts.drop_dirs else (),
    )


# meiva:note
# What the operator reads before the prompt: what happens, and, with `--drop-db`, exactly
# which directories go. The prompt and the removal share one list, so the text names exactly
# what is deleted.
def destroy_prompt(facts: DestroyFacts) -> tuple[str, ...]:
    role_text = ", ".join(facts.roles) if facts.roles else "no roles recorded"
    lines = [
        f"meiva cluster destroy will decommission the cluster at {facts.home} ({role_text}):",
        "  - stop its services and its own Postgres and Redis",
        "  - retire this host's OS jobs (launchd, crontab, boot unit) and the permissions helper",
        "  - mark the home detached: `meiva start` refuses it until destroy-intent.json is deleted",
    ]
    if "gateway" in facts.roles:
        lines.append("  - this host serves the gateway: every agent-runner loses its control plane")
    if facts.drop_dirs:
        lines.append("  - DELETE the cluster's database and cache, which cannot be undone:")
        lines.extend(f"      {directory}" for directory in facts.drop_dirs)
    return tuple(lines)


# meiva:note
# Only the exact home path confirms; anything else (an empty answer, end of input, "y")
# changes nothing.
def answer_confirms(answer: str, home: str) -> bool:
    return answer.strip() == home


def cleanup_incomplete(detail: str) -> str:
    return f"cluster cleanup incomplete: {detail}"


# meiva:note
# Every job removal runs even when an earlier one failed; an unavailable scheduler is
# ambiguous custody, so the failures are raised together once all were tried.
def scheduled_jobs_failure(failed: Sequence[tuple[str, str]]) -> str:
    return "could not remove scheduled jobs: " + ", ".join(
        f"{name} ({detail})" for name, detail in failed
    )

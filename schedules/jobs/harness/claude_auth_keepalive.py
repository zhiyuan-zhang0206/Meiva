# meiva:note
# One round of the Claude Code login keepalive: run the smallest possible `claude -p` so the
# CLI refreshes its own login and writes the rotated credentials back. This round never calls the
# OAuth refresh endpoint and never reads or writes a credentials file: the refresh token is
# single-use, and a second refresher would race a running CLI for it.
#
# A round never raises. A missing binary or a logged-out machine is a state of the machine, and a
# command that fails once will fail again; the schedule loop retries a raising round every few
# minutes, which here would be a model call per retry. The outcome is the log line and the
# `claude_auth_keepalive_round` event, whose status is `failed` for every reason but success.
# meiva:link schedules/README.md

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from base.daemon.schedules.inputs import ScheduleInputs
from base.host.env.harness.convergence import CommandResult
from base.lm.harness.claude import SECRET_ENV_NAMES
from base.host.env.harness_binaries import locate_harness_binary, with_harness_path
from schedules.jobs.telemetry_event import emit_system_event
from schedules.plans.harness.claude_keepalive import (
    KEEPALIVE_PROMPT,
    KEEPALIVE_TIMEOUT_S,
    STATUS_TIMEOUT_S,
    TIMEOUT_EXIT_CODE,
    KeepaliveDecision,
    decide_run,
    decide_without_run,
    keepalive_argv,
    logged_in,
    login_status_argv,
)

_PROCESS_NAME = "claude-auth-keepalive"
_EVENT_NAME = "claude_auth_keepalive_round"

# meiva:note
# The credential variables the claude launcher already names (`SECRET_ENV_NAMES`) plus the one it
# does not: `ANTHROPIC_AUTH_TOKEN`. Any of them in the child environment makes the CLI use it instead
# of the subscription login, so the keepalive would refresh nothing and bill the API.
_CREDENTIAL_ENV_NAMES = (*SECRET_ENV_NAMES, "ANTHROPIC_AUTH_TOKEN")


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str],
        cwd: Path,
        stdin: str | None,
    ) -> CommandResult: ...


def default_command_runner(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str],
    cwd: Path,
    stdin: str | None,
) -> CommandResult:
    try:
        res = subprocess.run(
            list(argv),
            input=stdin,
            timeout=timeout,
            capture_output=True,
            text=True,
            env=dict(env),
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            exit_code=TIMEOUT_EXIT_CODE,
            stdout=exc.stdout if isinstance(exc.stdout, str) else "",
            stderr=exc.stderr if isinstance(exc.stderr, str) else f"timed out after {timeout}s",
        )
    except OSError as exc:
        return CommandResult(exit_code=127, stdout="", stderr=f"cannot execute: {exc}")
    return CommandResult(exit_code=res.returncode, stdout=res.stdout or "", stderr=res.stderr or "")


# meiva:note
# Where the claude executable is on this machine, searching the PATH the round runs with and then the
# known install directories under the user's home; `None` when it is nowhere.
def locate_claude(user_home: Path, env: Mapping[str, str]) -> str | None:
    found, _ = locate_harness_binary("claude", home=user_home, path_env=env.get("PATH"))
    return found


def _decide(
    *,
    user_home: Path,
    base_env: Mapping[str, str],
    is_production: bool,
    locate: Callable[[Path, Mapping[str, str]], str | None],
    run_command: CommandRunner,
) -> KeepaliveDecision:
    if not is_production:
        return decide_without_run("non_production")
    env = {
        name: value
        for name, value in with_harness_path(base_env, home=user_home).items()
        if name not in _CREDENTIAL_ENV_NAMES
    }
    claude = locate(user_home, env)
    if claude is None:
        return decide_without_run("claude_not_found")
    status = run_command(
        login_status_argv(claude), timeout=STATUS_TIMEOUT_S, env=env, cwd=user_home, stdin=None
    )
    if logged_in(status.stdout) is False:
        return decide_without_run("not_logged_in")
    result = run_command(
        keepalive_argv(claude),
        timeout=KEEPALIVE_TIMEOUT_S,
        env=env,
        cwd=user_home,
        stdin=KEEPALIVE_PROMPT,
    )
    return decide_run(result.exit_code, result.stdout, result.stderr)


def execute_round(
    slot: datetime,
    _payload: str | None,
    *,
    inputs: ScheduleInputs,
    user_home: Path,
    base_env: Mapping[str, str],
    is_production: bool,
    locate: Callable[[Path, Mapping[str, str]], str | None],
    run_command: CommandRunner,
) -> None:
    decision = _decide(
        user_home=user_home,
        base_env=base_env,
        is_production=is_production,
        locate=locate,
        run_command=run_command,
    )
    print(f"[{slot.isoformat()}] {_PROCESS_NAME}: {decision.log_line}")
    emit_system_event(
        _EVENT_NAME,
        decision.telemetry_payload,
        process=_PROCESS_NAME,
        producer=inputs.producer,
        machine_reader=inputs.machine.name,
        image=inputs.image,
        home=inputs.home,
    )

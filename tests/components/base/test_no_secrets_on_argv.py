"""One guard for the whole class: no launch path may put secret material on a
command line (issue #974).

`ps -eo command` shows every process's argv to any local user on macOS and Linux
alike — a wider surface than an environment, which is owner-only on both. So
every place Meiva starts a long-running process is driven here with a sentinel
secret in the environment, and the argv it builds is asserted clean. A future
launcher that reaches for `redis-cli -a`, an env-var argv splice, or an
argv-carried JSON blob fails here rather than in a `ps` listing.

The schedule launcher contract lives with the schedule-manager tests and shares
the sentinel environment and real PTY argv harness from base.sessions.pty.tests.secret_argv.

Coverage is per *launcher*, not per caller: the `meiva start` session launch,
schedule processes and agent shells each funnel into one of the functions below
(the Redis bring-up is covered beside its code, in
`cli/commands/data_plane/tests/instance/test_redis_bringup_argv.py`). Schedule and agent-shell sessions are created through the
pty-sessions service: their environment travels in the request body of its
owner-only unix socket, so the service process and every shell it spawns carry
no secret on their argv. Agent-host startup also passes its secret-bearing DB
projection through the environment.
"""

from __future__ import annotations

from pathlib import Path

import psutil

from base.clock import SYSTEM_TIMERS
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.pty.tests.pty_service import PtyServiceProcess, service_socket
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_shells import output_until, type_line
from base.sessions.pty.tests.secret_argv import (
    SECRET_VALUES,
    assert_clean,
    assert_service_tree_clean,
)
from base.sessions.pty.tests.secret_argv import secret_env as secret_env
from base.sessions.pty.tests.secret_argv import secrets_in_creator_env as secrets_in_creator_env
from base.sessions.tests.poll_until import poll_until


def test_session_backend_new_session(unit_home: Path, secrets_in_creator_env: None) -> None:
    """`meiva start` / `meiva restart` bring every daemon up through here."""
    from base.host.env.registry import process_environment
    from base.sessions.backend import PosixProcSessionBackend
    from base.sessions.env_forwarding import forward_env_dict
    from base.sessions.record import SessionRecord

    backend = PosixProcSessionBackend(SYSTEM_TIMERS, unit_home)
    backend.new_session(
        "meiva-gateway",
        "sleep 60",
        unit_home,
        env=forward_env_dict(process_environment()),
    )
    try:
        record = SessionRecord.read(unit_home / "run" / "sessions" / "meiva-gateway.json")
        assert record is not None
        cmdline = psutil.Process(record.pid).cmdline()
        assert_clean([record.cmd, *cmdline], label="PosixProcSessionBackend.new_session")
    finally:
        backend.kill_session("meiva-gateway")


def test_launch_record_cannot_reach_a_live_process(
    unit_home: Path, secrets_in_creator_env: None
) -> None:
    from base.host.env.registry import process_environment
    from base.sessions import posixproc
    from base.sessions.backend import PosixProcSessionBackend
    from base.sessions.env_forwarding import forward_env_dict

    backend = PosixProcSessionBackend(SYSTEM_TIMERS, unit_home)
    backend.new_session(
        "meiva-gateway",
        "true",
        unit_home,
        env=forward_env_dict(process_environment()),
    )
    try:
        poll_until(
            lambda: not posixproc.has_session("meiva-gateway", home=unit_home),
            timeout=5,
            what="exited session record no longer resolves to a live process",
        )
        assert not posixproc.has_session("meiva-gateway", home=unit_home)
    finally:
        backend.kill_session("meiva-gateway")


def _shell_env_report(name: str) -> str:
    """What a secret-name lookup in the session's shell prints (empty: none is set)."""
    type_line(
        name,
        "printenv MEIVA_CLUSTER_SECRET MEIVA_DB_URL MEIVA_REDIS_URL DEEPSEEK_API_KEY; echo ENVCHECK_DONE",
    )
    return output_until(name, "ENVCHECK_DONE")


def test_agent_shell_session(
    pty_service: PtyServiceProcess,
    database: Database,
    unit_home: Path,
    secrets_in_creator_env: None,
) -> None:
    """An agent's own persistent shell. The create is a `new` request to the
    pty-sessions service carrying the host-scope session forward view in its body
    (the allowlist deliberately drops the cluster-scope secrets: the shell's
    children re-source them at their own boot), so the secret values appear
    NOWHERE in this launch path: not on an argv of the service or the shell, not in
    the shell's environment."""
    from base.paths import workspace_dir
    from base.sessions.backend import PtySessionBackend, get_shell_backend
    from base.sessions.pty.client import PtyClientConfig
    from meiva.shell.sessions import ShellSessions, session_host
    from meiva.shell.tests.live_shell import seed_agent

    seed_agent(database, 1, next_index=3)
    ShellSessions(
        backend=PtySessionBackend(PtyClientConfig(socket_path=service_socket()), unit_home),
        database=database,
        agent_id=1,
    ).create("probe", cwd=workspace_dir(1, home=unit_home), ttl=120, host=session_host())
    name = "meiva-agent-1-shell-3-probe"
    assert get_shell_backend(home=unit_home).has_session(name)

    report = _shell_env_report(name)
    for secret in SECRET_VALUES:
        assert secret not in report, f"{secret!r} leaked into the shell's environment"
    assert_service_tree_clean(pty_service, label="meiva.shell.sessions.create_session")

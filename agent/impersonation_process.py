"""Bound relay process launch and credential delivery."""

from __future__ import annotations

import contextlib
import subprocess
import sys

__all__ = ["relay_argv", "spawn_codex_relay"]


# meiva:note
# The command line carries lease identifiers and record paths only: the scoped
# relay token arrives over stdin (``--token-stdin``), and an Antigravity CSRF token
# never travels at all because the relay reads the owner-only record this path names.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def relay_argv(
    agent_id: int,
    lease_id: str,
    thread_id: str,
    codex_remote: str | None,
    *,
    provider: str = "codex",
    antigravity_endpoint_file: str | None = None,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "cli",
        "impersonate",
        "relay",
        str(agent_id),
        "--lease-id",
        lease_id,
        "--provider",
        provider,
        "--thread-id",
        thread_id,
        "--token-stdin",
    ]
    if codex_remote is not None:
        argv.extend(["--codex-remote", codex_remote])
    if antigravity_endpoint_file is not None:
        argv.extend(["--antigravity-endpoint-file", antigravity_endpoint_file])
    return argv


def spawn_codex_relay(
    agent_id: int,
    lease_id: str,
    relay_token: str,
    thread_id: str,
    codex_remote: str | None,
    codex_home: str | None = None,
    *,
    provider: str = "codex",
    antigravity_endpoint_file: str | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn the bound relay; the scoped credential travels over a private pipe.

    The token never appears in argv or the environment. The child receives the
    session environment projection and boots its cluster connections. Stdout is
    discarded because delivery uses the host IPC path; stderr flows into this
    process's log.
    """
    from base.sessions.env_forwarding import forward_env_dict

    relay_env = forward_env_dict()
    if codex_home is not None:
        relay_env["CODEX_HOME"] = codex_home
    # meiva:note
    # The projection carries no host-injected ANTIGRAVITY_* variables, so an
    # Antigravity relay reads the endpoint record the request wrote (by path) instead
    # of discovering one of its own.
    # meiva:link docs/conventions/agents/agent-impersonation-hosts.md
    argv = relay_argv(
        agent_id,
        lease_id,
        thread_id,
        codex_remote,
        provider=provider,
        antigravity_endpoint_file=antigravity_endpoint_file,
    )
    process = subprocess.Popen(  # noqa: S603 — fixed argv built from constants and lease-row fields
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
        env=relay_env,
    )
    with contextlib.suppress(BrokenPipeError, OSError):  # child already gone; poll reports it
        assert process.stdin is not None  # noqa: S101 — PIPE requested above
        with process.stdin:
            process.stdin.write(relay_token.encode() + b"\n")
    return process

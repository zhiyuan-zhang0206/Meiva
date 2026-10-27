from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Mapping

from agent.takeover.relay import RelayBinding, relay_command

__all__ = ["spawn_relay"]


# meiva:note
# Spawn the bound relay; the scoped credential travels over a private pipe. The token
# never appears in argv or the environment. The child receives the session environment
# projection and boots its cluster connections. Stdout is discarded because delivery uses
# the host IPC path; stderr flows into this process's log. The argv is fixed text built
# from the program prefix and lease-row fields. A child that is already gone is reported
# by its exit poll, not by the pipe write.
# meiva:link agent/docs/impersonation/relay-supervision.meiva.okf.md
def spawn_relay(
    program: tuple[str, ...], binding: RelayBinding, *, token: str, environ: Mapping[str, str]
) -> subprocess.Popen[bytes]:
    from base.sessions.env_forwarding import forward_env_dict

    command = relay_command(program, binding, token=token, forwarded_env=forward_env_dict(environ))
    process = subprocess.Popen(  # noqa: S603
        command.argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
        env=dict(command.env),
    )
    with contextlib.suppress(BrokenPipeError, OSError):
        assert process.stdin is not None  # noqa: S101
        with process.stdin:
            process.stdin.write(command.stdin)
    return process

# meiva:note
# Where a remote agent-runner dials this gateway, and how a browser logs in.
#
# The gateway's own `.env` holds the loopback URL (a box reaches its own gateway over loopback);
# the hint names the OTHER address, so whoever just brought the gateway up can enroll runners
# against it without hunting for the host and port.

from __future__ import annotations

from pathlib import Path

from base.host.net.predicates import is_loopback_host

__all__ = ["gateway_hint_lines"]


# meiva:note
# The lines a ready gateway prints: the web login password's place, then its address.
def gateway_hint_lines(*, host: str, port: int, home: Path) -> tuple[str, ...]:
    login = (
        f"\n→ web login password: the MEIVA_CLUSTER_SECRET value in {home / '.env'} "
        "(the CLI, SDK and local services present their own credentials)"
    )
    if is_loopback_host(host):
        return (
            login,
            f"\n→ gateway reachable at http://{host}:{port} (loopback only — set "
            "MEIVA_MACHINE_HOST to this box's private-network address to enroll remote "
            "agent-runners)",
        )
    reachable = f"http://{host}:{port}"
    return (
        login,
        f"\n→ gateway reachable at {reachable}",
        f"  join an agent-runner: meiva init --serve-agent-runner --no-serve-gateway "
        f"--gateway-url {reachable} --machine-name <name> --machine-host <runner-host> "
        "--db-capability <bundle>, then meiva start (bundle from `meiva cluster db-authority "
        "issue-unit` here; MEIVA_DB_CAPABILITY_KEY set from a non-echoing prompt)",
    )

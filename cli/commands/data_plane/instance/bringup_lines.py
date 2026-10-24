# meiva:note
# The words the gateway's data-plane bring-up prints about a remote-managed plane.

from __future__ import annotations

__all__ = [
    "remote_announce",
    "remote_pg_failure",
    "remote_ready",
    "remote_redis_failure",
]


# meiva:note
# Announce that the URLs name another host, so no local instance is brought up.
def remote_announce(host: str) -> str:
    return f"\n→ data plane remote-managed ({host}) — skipping local instance bring-up"


# meiva:note
# The gateway cannot serve at all when its Postgres is unreachable, so the failure carries the
# dial detail and the advice instead of a bare traceback from the first migration.
def remote_pg_failure(detail: str) -> str:
    return (
        f"  ✗ remote data plane unreachable: {detail}\n"
        "    Check MEIVA_DB_URL / MEIVA_REDIS_URL and their credentials, and the "
        "network path to the provider. A local cluster switching to a remote "
        "plane must first migrate its data (pg_dump / restore) and apply "
        "pending migrations here."
    )


# meiva:note
# An unreachable remote Redis, with its dial detail.
def remote_redis_failure(detail: str) -> str:
    return f"  ✗ remote data plane unreachable: {detail}"


# meiva:note
# Both remote services answered.
def remote_ready(pg_detail: str, redis_detail: str) -> str:
    return f"  ✓ remote data plane reachable ({pg_detail}; {redis_detail})"

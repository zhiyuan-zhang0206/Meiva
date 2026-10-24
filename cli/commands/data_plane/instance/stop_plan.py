# meiva:note
# What stopping this cluster's own data plane does and says, as values.
#
# A remote-managed plane has nothing to stop locally, but a cluster that switched local to
# remote may still have its old local instance running, so the shell looks and this module
# words the hint. A local plane stops its pooler first so clients are disconnected before
# Postgres goes down, then Postgres, then Redis as its admin user.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "StopPlane",
    "StopPooler",
    "StopPostgres",
    "StopRedis",
    "orphan_warning",
    "plan_stop",
    "redis_stop_failure",
    "remote_stop_notice",
]


# meiva:note
# Stop the pooler (best effort, a no-op when it was never enabled).
@dataclass(frozen=True)
class StopPooler: ...


# meiva:note
# Stop Postgres under native custody, reporting an escalation.
@dataclass(frozen=True)
class StopPostgres: ...


# meiva:note
# Shut Redis down on `port` as its admin user and verify it no longer answers.
@dataclass(frozen=True)
class StopRedis:
    port: int


StopPlane = StopPooler | StopPostgres | StopRedis


# meiva:note
# The steps that stop a local plane; Redis only when the URL names a port.
def plan_stop(*, redis_port: int | None) -> tuple[StopPlane, ...]:
    redis: tuple[StopPlane, ...] = () if redis_port is None else (StopRedis(redis_port),)
    return (StopPooler(), StopPostgres(), *redis)


# meiva:note
# What a remote-managed plane prints on stop.
def remote_stop_notice(host: str) -> str:
    return f"\n→ data plane remote-managed ({host}) — nothing to stop locally"


# meiva:note
# The hint for a local instance left behind by a local-to-remote switch, or `None`.
#
# The signal is a live process answering on this cluster's own instance ports. `meiva stop`
# skips it, so it keeps consuming the home's ports and data dir until torn down by hand.
def orphan_warning(*, postgres_running: bool, redis_listening: bool) -> str | None:
    leftovers = [
        name
        for name, running in (("postgres", postgres_running), ("redis", redis_listening))
        if running
    ]
    if not leftovers:
        return None
    return (
        "  ⚠ local " + " + ".join(leftovers) + " from before the switch is still "
        "running on this home's data dir and is no longer managed (the data "
        "plane is remote). Tear it down by hand — `pg_ctl -D $MEIVA_HOME/pg "
        "-m fast stop` and `redis-cli -p <port> shutdown nosave` — or switch "
        "the URLs back to local and stop it with `meiva stop` (see `meiva stop --help`)."
    )


# meiva:note
# The exit status of `shutdown` proves nothing; Redis stopped only when it no longer answers.
def redis_stop_failure(port: int, *, still_answers: bool) -> str | None:
    if not still_answers:
        return None
    return f"redis still answers on 127.0.0.1:{port} after shutdown"

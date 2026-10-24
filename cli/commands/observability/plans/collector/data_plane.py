# meiva:note
# The Postgres and Redis receivers a gateway's collector scrapes its own data plane with.
#
# The shell in `otel_collector.py` reads the registry record, the socket path and the database
# URL; these functions decide which receivers exist and write their config blocks. A pure
# agent-runner's URLs point at the GATEWAY's data plane, so scraping from there would duplicate
# the gateway's own series under a second host label: it gets no receiver.

from __future__ import annotations

from dataclasses import dataclass

from cli.commands.observability.plans.collector.text import yaml_quote
from cli.commands.observability.plans.url_view import UrlView

__all__ = [
    "PEER_PLACEHOLDER",
    "DataPlanePlan",
    "data_plane_fragments",
    "plan_data_plane_receivers",
    "postgres_receiver_block",
    "receiver_endpoint",
    "redis_receiver_block",
]

# meiva:note
# Not a credential: `pg_hba` admits the monitoring role only by peer, and the role has no
# verifier, so no password can ever authenticate it.
PEER_PLACEHOLDER = "peer-authenticated-no-password"

# meiva:note
# The receiver blocks are written into the collector config verbatim, comments included, so the
# rendered file explains itself to the operator who reads it. The Postgres scrape interval is 60s,
# not the host metrics' 30s: connection counts and Redis memory are slow-moving pressure gauges.
_POSTGRES_RECEIVER_BLOCK = """
  # This cluster's OWN Postgres over the home's owner-only unix socket, never
  # through PgBouncer: the receiver reads pg_stat_* views, which a
  # transaction-pooled session cannot be trusted to serve consistently. It logs
  # in as the stable monitoring role by peer (the collector runs as the home's
  # OS user): that role has no password and is not a write generation, so no
  # credential lives here and a rollout neither revokes nor rotates it. The
  # contrib receiver refuses an empty password, so the value below is a fixed
  # non-secret placeholder that peer authentication never asks for. The
  # receiver prefixes a unix endpoint's host with "/".
  postgresql:
    endpoint: {pg_endpoint}
    transport: unix
    username: {pg_user}
    password: {pg_placeholder}
    databases: [{pg_database}]
    collection_interval: 60s
    tls:
      insecure: true
"""

_REDIS_RECEIVER_BLOCK = """
  # This cluster's OWN Redis. The receiver dials the default administrative
  # user, so it uses the gateway-only Redis admin password; an empty password
  # is the no-auth single-box default and the receiver skips AUTH.
  redis:
    endpoint: {redis_endpoint}
    transport: tcp
    password: {redis_password}
    collection_interval: 60s
"""


# meiva:note
# Which data-plane receivers this unit's collector carries.
@dataclass(frozen=True)
class DataPlanePlan:
    postgres: bool
    redis: bool


# meiva:note
# The receivers a unit's collector scrapes.
#
# None on anything that does not own Postgres and Redis: an unconfigured unit (`roles` None) has
# no URLs to read, and a unit without the gateway capability scrapes the gateway's plane. A
# never-dialed placeholder database URL means no plane exists yet. Redis stays observable in every
# posture; the Postgres receiver exists only for a home-owned instance with OTLP export on, since a
# remote-managed plane has no owner-only socket or monitoring role here.
def plan_data_plane_receivers(
    *,
    roles: frozenset[str] | None,
    placeholder_db_url: bool,
    otlp_enabled: bool,
    remote_plane: bool,
) -> DataPlanePlan:
    if roles is None or "gateway" not in roles or placeholder_db_url:
        return DataPlanePlan(postgres=False, redis=False)
    return DataPlanePlan(postgres=otlp_enabled and not remote_plane, redis=True)


# meiva:note
# `host:port` for a receiver, or an explosion naming what to fix.
#
# A connection URL with no port cannot be scraped, and guessing the default would point the
# receiver at whatever else listens there.
def receiver_endpoint(url: UrlView, what: str) -> str:
    if url.hostname is None or url.port is None:
        raise RuntimeError(
            f"cannot build the otel-collector {what} receiver: {what} URL has no "
            f"host:port (got {url.scheme}://{url.netloc!r})"
        )
    return f"{url.hostname}:{url.port}"


# meiva:note
# The Postgres receiver block for this home's own instance: its socket directory (with the
# leading slash removed, as the receiver re-adds it), the port, the monitoring role and the
# database name.
def postgres_receiver_block(*, socket_dir: str, port: int, user: str, database: str) -> str:
    return _POSTGRES_RECEIVER_BLOCK.format(
        pg_endpoint=yaml_quote(f"{socket_dir.lstrip('/')}:{port}"),
        pg_user=yaml_quote(user),
        pg_placeholder=yaml_quote(PEER_PLACEHOLDER),
        pg_database=yaml_quote(database),
    )


# meiva:note
# The Redis receiver block for an endpoint and the admin password.
def redis_receiver_block(*, endpoint: str, password: str) -> str:
    return _REDIS_RECEIVER_BLOCK.format(
        redis_endpoint=yaml_quote(endpoint), redis_password=yaml_quote(password)
    )


# meiva:note
# The receiver blocks and the pipeline-list fragment for a plan: the text rendered into
# `$DATA_PLANE_RECEIVERS` and `$DATA_PLANE_PIPELINE_RECEIVERS`.
def data_plane_fragments(
    plan: DataPlanePlan, *, postgres_block: str, redis_block: str
) -> tuple[str, str]:
    blocks: list[str] = []
    receivers: list[str] = []
    if plan.postgres:
        blocks.append(postgres_block)
        receivers.append("postgresql")
    if plan.redis:
        blocks.append(redis_block)
        receivers.append("redis")
    return "".join(blocks), "".join(f", {receiver}" for receiver in receivers)

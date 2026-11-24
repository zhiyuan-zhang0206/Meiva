# meiva:note
# The Postgres, Redis and PgBouncer-stats receivers a gateway's collector scrapes its own data plane
# with.
#
# The shell in `otel_collector.py` reads the registry record, the socket path and the database
# URL; these functions decide which receivers exist and write their config blocks. A pure
# agent-runner's URLs point at the GATEWAY's data plane, so scraping from there would duplicate
# the gateway's own series under a second host label: it gets no receiver.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli.commands.observability.plans.collector.text import yaml_quote
from cli.commands.observability.plans.url_parts import UrlParts

__all__ = [
    "PEER_PLACEHOLDER",
    "DataPlaneFragments",
    "DataPlanePlan",
    "data_plane_fragments",
    "pgbouncer_blocks",
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

_PGBOUNCER_RECEIVER_BLOCK = """
  # PgBouncer's per-minute `LOG stats:` line becomes two gauges: transactions
  # per second and the average query time in microseconds. The source is the
  # pooler's own log, not its admin console: `SHOW STATS` needs the SCRAM admin
  # password over TCP, while the log needs no credential and carries the numbers
  # the retired `db-write-stall-watch` schedule read. The stall rule itself (under 20
  # transactions/s with an average query above 300 ms for 3 minutes) lives in
  # Grafana; the collector only emits the signal. The lines go through their own
  # pipeline into a connector, never the main logs pipeline, so they do not
  # reach Loki; any other log line fails the regex and is dropped quietly.
  filelog/pgbouncer:
    include:
      - {log_path}
    start_at: end
    storage: file_storage/logoffsets
    poll_interval: 10s
    operators:
      - type: regex_parser
        on_error: drop_quiet
        regex: '^\\d{{4}}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d\\.\\d+ \\S+ \\[\\d+\\] LOG stats: (?P<xacts>\\d+) xacts/s, \\d+ queries/s,.*?, xact \\d+ us, query (?P<query_us>\\d+) us, wait \\d+ us'
"""

_PGBOUNCER_CONNECTOR_BLOCK = """  signal_to_metrics/pgbouncer:
    logs:
      - name: pgbouncer.transactions
        description: Transactions per second in the latest PgBouncer stats line.
        unit: "{transaction}/s"
        gauge:
          value: Double(log.attributes["xacts"])
      - name: pgbouncer.query_time
        description: Average query time in the latest PgBouncer stats line.
        unit: us
        gauge:
          value: Double(log.attributes["query_us"])
"""

_PGBOUNCER_PIPELINE_BLOCK = """    logs/pgbouncer:
      receivers: [filelog/pgbouncer]
      processors: [memory_limiter]
      exporters: [signal_to_metrics/pgbouncer]"""


# meiva:note
# Which data-plane receivers this unit's collector carries. PgBouncer's log is read on exactly the
# units that scrape their own Postgres: both exist only where this home owns the data plane.
@dataclass(frozen=True)
class DataPlanePlan:
    postgres: bool
    redis: bool
    pgbouncer: bool = False


@dataclass(frozen=True)
class DataPlaneFragments:
    receivers: str = ""
    connectors: str = ""
    pipelines: str = ""
    infra_receivers: str = ""


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
    owned = otlp_enabled and not remote_plane
    return DataPlanePlan(postgres=owned, redis=True, pgbouncer=owned)


# meiva:note
# `host:port` for a receiver, or an explosion naming what to fix.
#
# A connection URL with no port cannot be scraped, and guessing the default would point the
# receiver at whatever else listens there.
def receiver_endpoint(url: UrlParts, what: str) -> str:
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


def pgbouncer_blocks(*, log_path: Path) -> tuple[str, str, str]:
    return (
        _PGBOUNCER_RECEIVER_BLOCK.format(log_path=yaml_quote(log_path.as_posix())),
        _PGBOUNCER_CONNECTOR_BLOCK,
        _PGBOUNCER_PIPELINE_BLOCK,
    )


# meiva:note
# The config fragments for a plan, in the order the infra metrics pipeline lists its receivers.
def data_plane_fragments(
    plan: DataPlanePlan,
    *,
    postgres_block: str,
    redis_block: str,
    pgbouncer: tuple[str, str, str],
) -> DataPlaneFragments:
    blocks: list[str] = []
    receivers: list[str] = []
    if plan.postgres:
        blocks.append(postgres_block)
        receivers.append("postgresql")
    if plan.redis:
        blocks.append(redis_block)
        receivers.append("redis")
    if not plan.pgbouncer:
        return DataPlaneFragments(
            receivers="".join(blocks),
            infra_receivers="".join(f", {receiver}" for receiver in receivers),
        )
    receiver_block, connector_block, pipeline_block = pgbouncer
    receivers.append("signal_to_metrics/pgbouncer")
    return DataPlaneFragments(
        receivers="".join([*blocks, receiver_block]),
        connectors=connector_block,
        pipelines=pipeline_block,
        infra_receivers="".join(f", {receiver}" for receiver in receivers),
    )

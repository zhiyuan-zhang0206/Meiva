# meiva:note
# This host's snapshot in the multi-machine view, assembled from facts already read.
#
# `GET /api/cluster/status`, the `meiva cluster status` CLI and the status page consume the
# same `ClusterStatus` payload. The shell in `ops.cluster_status` reads the pidfiles, the
# session backends, the database and the resource sample once and hands them here as
# `StatusFacts`; this module decides what the snapshot says and does no I/O.

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from base.api_contracts.status import PausedReason, SchemaMismatchStatus
from base.host.resource_sample import ResourceSample
from ops.cluster_status.model import count_agent_shells, group_agent_sessions
from ops.rpc_schemas import SessionInfo

__all__ = [
    "ClusterStatus",
    "DbSnapshot",
    "StatusFacts",
    "assemble_status",
    "paused_reason",
    "serving_gate_reason",
    "unavailable_db_snapshot",
]


# meiva:note
# `GET /api/cluster/status` response body — this host's snapshot in the multi-machine view.
#
# `meiva cluster status` CLI and the future monitoring page consume the same payload.
class ClusterStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_name: str
    # meiva:note
    # Three orthogonal capability flags (any combination on a single host) — never a single
    # categorical "role". `serve_observability_station` defaults False so a client on
    # pre-station code still parses a station host's snapshot.
    # meiva:link base/cluster/machine/docs/machine.meiva.okf.md
    serve_gateway: bool
    serve_agent_runner: bool
    serve_observability_station: bool = False
    # meiva:note
    # Status only: an unreadable database snapshot or a serving gate that has not reached
    # `serving` — the compound reads true whenever this host cannot claim readiness,
    # whichever clause fired.
    paused: bool
    # meiva:note
    # Which clause of the `paused` verdict fired — the first true one, in the verdict's own
    # order: no_state / startup. None when not paused. Splitting it apart keeps a failed
    # start's parked serving gate (`startup`) legible from a failed database read, instead of
    # one opaque bool that a consumer can only guess at.
    paused_reason: PausedReason | None = None
    # meiva:note
    # This host's prod-source HEAD commit (`$MEIVA_HOME/source`), or None when it cannot be
    # read (no prod source / git unavailable); threaded to the roster so the multi-machine
    # view shows each node's checkout.
    head_sha: str | None = None
    # meiva:note
    # The commit the process answering this probe actually loaded, frozen at its own boot
    # (`base.native_process.loaded_commit`), or None when it never froze one. Distinct from
    # head_sha: head_sha is the checkout, running_sha is code the live process holds. They
    # differ when the checkout advanced (`git pull`) but the process was not restarted — the
    # roster marks that node's code as stale.
    #
    # This speaks only for the answering process (the ops daemon on an agent-runner, the
    # gateway on a pure gateway). A sibling daemon respawned at a different commit is not
    # covered here; its own commit is on its `/healthz`, and `probe_daemon` surfaces it per
    # daemon.
    running_sha: str | None = None
    schema_mismatch: SchemaMismatchStatus | None = None
    # meiva:note
    # This host's live agent shell-session count, surfaced per-machine in the status panel.
    # 0 on a host with no agents (e.g. a pure gateway).
    shell_count: int = 0
    # meiva:note
    # Per-host daemon liveness (pidfile + signal). None = could not probe. Shown per-machine
    # in the roster; central-only daemons (labeler/memory-indexer) are not here — they live
    # in the gateway services panel.
    agent_host_online: bool | None = None
    supervisor_online: bool | None = None
    # meiva:note
    # Agent-runner detail surfaced on the Status Page. `agent_count` is this host's
    # non-terminated agent identities, including idle and paused agents. `session_count`
    # counts live service and persistent shell sessions.
    agent_count: int = 0
    session_count: int = 0
    # meiva:note
    # The agents' shell/watcher sessions grouped by agent for hierarchical display (the agent
    # process itself is not a session, so it is not a group entry).
    agent_groups: list[dict[str, object]] = []
    # meiva:note
    # This machine's live CPU / memory / disk reading — one sample, no history (Prometheus
    # holds the series). None when psutil is unavailable.
    resource: ResourceSample | None = None


# meiva:note
# What one bounded read of the central database produced. `ok` says whether the read
# succeeded: a host whose database snapshot failed cannot claim readiness.
@dataclass(frozen=True)
class DbSnapshot:
    ok: bool
    agent_count: int
    schema_mismatch: SchemaMismatchStatus | None


# meiva:note
# Every fact one snapshot is made of, read once by the shell. `agent_host_online` is None on
# a host that is not an agent-runner; `serving` is the start-serving gate's verdict.
@dataclass(frozen=True)
class StatusFacts:
    machine_name: str
    roles: frozenset[str]
    head_sha: str | None
    running_sha: str | None
    sessions: tuple[SessionInfo, ...]
    agent_host_online: bool | None
    supervisor_online: bool | None
    db: DbSnapshot
    resource: ResourceSample | None
    serving: bool


# meiva:note
# `startup` while this host's serving gate has not reached `serving`, else None.
#
# A start is in flight, a stop has begun, or a failed start parked the gate and recovery stays
# gated. A local fact: no database read.
def serving_gate_reason(*, serving: bool) -> PausedReason | None:
    return None if serving else "startup"


# meiva:note
# The first true clause of the `paused` verdict, in its own clause order.
#
# `no_state` (the database snapshot read failed), then `startup` (see `serving_gate_reason`).
# None when neither fired.
def paused_reason(*, db_ok: bool, serving: bool) -> PausedReason | None:
    if not db_ok:
        return "no_state"
    return serving_gate_reason(serving=serving)


# meiva:note
# The degraded database snapshot: the read failed, so the host cannot claim readiness and
# reports no agents. The diagnosis says why instead of leaving the schema silently unchecked.
def unavailable_db_snapshot(machine_name: str, error_type: str) -> DbSnapshot:
    return DbSnapshot(
        ok=False,
        agent_count=0,
        schema_mismatch=SchemaMismatchStatus(
            kind="unavailable",
            machine=machine_name,
            detail=f"schema comparison unavailable: status database snapshot failed ({error_type})",
        ),
    )


# meiva:note
# The snapshot these facts describe.
#
# The producer is typed (`AgentSessionGroup`); `ClusterStatus.agent_groups` stays an open dict
# list so the frontend-facing status schema (and its generated TS types) is unchanged — the
# models serialize to JSON dicts here. `paused` is exactly "a clause fired", so the bool can
# never drift from its reason.
def assemble_status(facts: StatusFacts) -> ClusterStatus:
    reason = paused_reason(db_ok=facts.db.ok, serving=facts.serving)
    return ClusterStatus(
        machine_name=facts.machine_name,
        serve_gateway="gateway" in facts.roles,
        serve_agent_runner="agent-runner" in facts.roles,
        serve_observability_station="observability-station" in facts.roles,
        paused=reason is not None,
        paused_reason=reason,
        head_sha=facts.head_sha,
        running_sha=facts.running_sha,
        schema_mismatch=facts.db.schema_mismatch,
        shell_count=count_agent_shells(facts.sessions),
        agent_host_online=facts.agent_host_online,
        supervisor_online=facts.supervisor_online,
        agent_count=facts.db.agent_count,
        session_count=len(facts.sessions),
        agent_groups=[g.model_dump(mode="json") for g in group_agent_sessions(facts.sessions)],
        resource=facts.resource,
    )

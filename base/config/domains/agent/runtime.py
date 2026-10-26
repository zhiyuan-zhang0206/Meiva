from __future__ import annotations

from typing import Literal

from pydantic import Field

from base.config.base import EnvSettings
from base.config.domains.agent.completion_policy import CompletionNoticePolicy


class AgentRuntimeSettings(EnvSettings):
    completion_notice_policy: Literal[CompletionNoticePolicy.ALL, CompletionNoticePolicy.HOURLY] = (
        Field(
            default=CompletionNoticePolicy.ALL,
            alias="MEIVA_COMPLETION_NOTICE_POLICY",
            description="Per-agent completion notification policy: all delivers one notice per completion, and hourly aggregates every completion in a durable hourly digest.",
            json_schema_extra={
                "restart_required": "agent",
                "writable": True,
                "sensitive": False,
                "scope": "cluster-default",
                "per_agent": True,
                "lifecycle": "live",
            },
        )
    )

    agent_host_memory_max_mb: int = Field(
        default=0,
        alias="MEIVA_AGENT_HOST_MEMORY_MAX_MB",
        description=(
            "Aggregate memory cap in MiB for the agent-host unit's whole process tree on"
            " Linux: 0 disables it. The host is started inside a transient systemd scope"
            " whose memory.max is this value, so a runaway worker inside it cannot starve"
            " the machine's shared applications. Size it as RAM minus (gateway + desktop +"
            " headroom) -- 18432 on a 31 GiB node -- and keep it above any per-exec cap,"
            " because a child scope counts against its parent (2026-10-22: an 8.29 GiB"
            " export killed chrome and typeless on the central node, #5543)."
        ),
        ge=0,
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    agent_host_memory_swap_max_mb: int = Field(
        default=0,
        alias="MEIVA_AGENT_HOST_MEMORY_SWAP_MAX_MB",
        description=(
            "Swap allowance in MiB for the same scope: 0 keeps the cap hard, so exceeding"
            " it frees memory instead of pushing the machine into swap. Only a MemoryMax"
            " without this lets an over-cap tree page forever, which protects nothing and"
            " still fills the disk."
        ),
        ge=0,
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    exec_memory_guard_floor_mb: int = Field(
        default=1024,
        alias="MEIVA_EXEC_MEMORY_GUARD_FLOOR_MB",
        description=(
            "Smallest exec footprint in MiB the host memory guard kills for at critical"
            " system memory pressure; at or below it the guard records a skip and kills"
            " nothing. The guard takes down a runaway, it does not free memory: a kill"
            " below this line costs the agent's work and returns no relief. 2026-10-26"
            " killed a 0.1 GiB exec while the system stayed critical, and 39% of that"
            " day's pressure samples were already critical, so the case is ordinary."
            " Keep it far under what a runaway reaches in seconds (a runaway passes 1 GiB"
            " long before it threatens the machine), and above the noise of ordinary work."
        ),
        ge=0,
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    exec_oom_score_adj: int = Field(
        default=800,
        alias="MEIVA_EXEC_OOM_SCORE_ADJ",
        description=(
            "OOM preference an execute_code child gives itself on Linux: 0 leaves it at the"
            " inherited score, 1000 makes it the kernel's first choice. A worker is rerunnable"
            " and the machine's browser, editor and desktop apps are not, yet systemd's user"
            " units already run at +300, so a worker left at 0 made the kernel kill the browser"
            " first (2026-10-22: an 8.29 GiB export on ubuntu killed chrome and typeless). Set"
            " 0 on a host where agent work is the main load and must not be preferred victims."
        ),
        ge=0,
        le=1000,
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    checkpoint_interval: int = Field(
        default=4,
        alias="MEIVA_CHECKPOINT_INTERVAL",
        description="Persist a LangGraph checkpoint every Nth super-step (4 by default; 1 restores every-step persistence). Crash recovery replays up to N-1 super-steps. Per-agent override, restart required.",
        gt=0,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    db_notify_wait_timeout_seconds: float = Field(
        default=30.0,
        alias="MEIVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS",
        description="Agent-host inbound subscription read timeout and durable pending-work scan interval (seconds). A lost Redis wake is recovered by the next database scan.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    node_stall_dump_seconds: float = Field(
        default=0.0,
        alias="MEIVA_NODE_STALL_DUMP_SECONDS",
        description="If > 0, dump every thread's stack to stderr when a graph node stays in one node longer than this many seconds (one-shot per node). 0 disables. A hang diagnostic; off in prod, on in the e2e harness.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    db_pool_acquire_timeout_seconds: float = Field(
        default=30.0,
        alias="MEIVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS",
        description="Max seconds an agent waits to borrow a Postgres connection before raising. Keep it generous: a mid-turn raise exits the agent process, which is not auto-resurrected.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    db_pool_slow_acquire_warn_seconds: float = Field(
        default=10.0,
        alias="MEIVA_DB_POOL_SLOW_ACQUIRE_WARN_SECONDS",
        description="A successful connection borrow at least this slow is reported as a `db_pool_acquire_slow` event (WARNING). A healthy borrow takes milliseconds, so seconds mean the host is stalled or Postgres is reconnecting; 3s fired on every host-stall check spike (2026-10-03 triage #8: 39 of 41 lines had check_ms == elapsed, a normal reset check being ~30ms). Stays below db_pool_acquire_timeout_seconds so a slow-but-served borrow is visible before it becomes a hard timeout.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    impersonation_ack_window_seconds: int = Field(
        default=180,
        alias="MEIVA_IMPERSONATION_ACK_WINDOW_SECONDS",
        description="Seconds to ACK each impersonation delivery attempt. Snapshotted when a lease is requested; config edits apply to new leases, including after relay restart.",
        gt=0,
        le=2147483647,
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    impersonation_max_delivery_attempts: int = Field(
        default=2,
        alias="MEIVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS",
        description="Maximum total delivery attempts per impersonation message, including the initial submission. Exhaustion pauses automatic delivery of that message; the lease and late ACK remain valid. Snapshotted at request; config edits apply to new leases.",
        gt=0,
        le=2147483647,
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    heartbeat_pause_max_seconds: float = Field(
        default=86400.0,
        alias="MEIVA_HEARTBEAT_PAUSE_MAX_SECONDS",
        description="Maximum seconds one meiva.self.pause_heartbeat(duration) call may pause the idle heartbeat. Cluster default; an agent can override it per-agent via its config overlay (e.g. meiva.self.restart(config_overlay=...)).",
        gt=0,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    understanding_enabled: bool = Field(
        default=False,
        alias="MEIVA_UNDERSTANDING_ENABLED",
        description="Master switch for chunk-triggered understanding. On: the llm node and the compact paths enqueue context chunks into `understanding_chunk_jobs`, and the agent host's consumer loop describes them into depth-1 `understanding_nodes` rows. Off ships the mechanism dark: nothing is enqueued and the loop idles.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understanding_chunk_ratio: float = Field(
        default=0.5,
        alias="MEIVA_UNDERSTANDING_CHUNK_RATIO",
        description="Understanding chunk size as a fraction of the agent model's soft compaction threshold (its compact-reminder tokens, with the agent's own overrides): a chunk is enqueued when a request's provider-reported input tokens exceed those at the previous cut of the same compaction segment by at least ratio x that threshold. A segment's closing remainder is enqueued at compaction regardless of size. A small ratio speeds up verification.",
        gt=0,
        le=1,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understanding_group_model: str = Field(
        default="",
        alias="MEIVA_UNDERSTANDING_GROUP_MODEL",
        description="Model for the upper-level understanding groupings (level 2 and above). Empty = the agent's own model (`agent_model_target`). Unlike the leaf chunks, a grouping request has no agent prefix, so it is not tied to the agent's model for cache parity.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understanding_group_reasoning: str = Field(
        default="",
        alias="MEIVA_UNDERSTANDING_GROUP_REASONING",
        description="Reasoning strength of the understanding grouping calls above level 1 (`group.py`; the level-1 groups are written by the chunk calls, on the agent's own model). Empty (default) = the model's own default tier, untouched. `off` = thinking disabled where the provider allows it; any other value is passed as the reasoning effort of providers that take one (deepseek `high` / `max`). Set only to override the vendor default.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understanding_group_check_open: int = Field(
        default=60,
        alias="MEIVA_UNDERSTANDING_GROUP_CHECK_OPEN",
        description="Upper-level grouping cadence: a level (level 1 and above) is offered to one grouping call each time its open nodes, those without a parent, number this many more than at its previous check, so the call can close several groups at once (any group size; the newest node stays open). Lower = more frequent, smaller calls.",
        ge=3,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understanding_group_check_decay: int = Field(
        default=3,
        alias="MEIVA_UNDERSTANDING_GROUP_CHECK_DECAY",
        description="How much the grouping cadence tightens per level: level k (1 = grouping the leaves) is checked each time its open nodes number max(6, `MEIVA_UNDERSTANDING_GROUP_CHECK_OPEN` / decay^(k-1)) more than at its previous check (60, 20, 7, 6, ... by default), and its `must_close` brake is three times that. A higher level fills far more slowly, so with one threshold for all levels the top trails the history by days. 1 = the same threshold at every level.",
        ge=1,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understanding_group_corrections: int = Field(
        default=2,
        alias="MEIVA_UNDERSTANDING_GROUP_CORRECTIONS",
        description="Correction rounds of one grouping reply: a chunk call whose groups carry invalid first/last unit numbers or do not cover the whole catalog without gaps, or an upper-level check whose ids are invalid or not consecutive, is sent back in the same conversation for the model to fix. Past this many corrections a chunk job fails (and is retried) or an upper-level check fails (its raw calls stay recorded; the level is checked again after `MEIVA_UNDERSTANDING_GROUP_CHECK_OPEN` more open nodes).",
        ge=0,
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

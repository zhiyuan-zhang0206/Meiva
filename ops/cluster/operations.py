# meiva:note
# Cluster-ops RPC implementations.
#
# Stopping announcements, live status snapshots, persistent-shell probes and the per-agent command
# view. One of the op clusters beside `ops.lifecycle`, `ops.host_config` and `ops.inventory`; each
# cluster is self-contained.
#
# Most of these are thin wrappers; this layer is the agent-runner-callable RPC surface the ops
# server dispatches (`services/agent_runner/agent_ops/daemon.py:_dispatch`) and the gateway
# cluster router calls.

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeGuard

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from base.agents.birth.harness import probe_local_harnesses
from base.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from base.api_contracts.agent_contract import ShellKillMode
from base.cluster.machine import MachineIdentityReader
from base.cluster.machines import mark_stopping
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from base.packages.composer_commands import Command
from base.packages.skills import project_skill_roots
from base.packages.skills.tree import Skill
from ops.cluster_status import (
    ClusterStatus,
    agent_shell_sessions,
    capture_shell,
    cluster_now,
    kill_shell,
    status_snapshot,
)
from ops.rpc_schemas import (
    AgentSkillViewResult,
    HarnessProbeResult,
    HarnessProbeRow,
    OpsCommandItem,
    ShellCaptureResult,
    ShellKillResult,
    ShellProbeResult,
)


# meiva:note
# Record an intentional shutdown announced by the (machine, home) unit.
#
# `meiva stop` calls this (best-effort) just before tearing the local stack down, so the cluster
# view shows the host as "stopped" rather than "offline" (a live probe cannot tell an intentional
# stop from a crash). Stamps the unit's `stopped_at` and recomputes the composed `machines` row;
# `meiva start` clears it. `home` is the stopping unit's $MEIVA_HOME, sent on the wire so a
# co-located peer's caps are not retracted along with this unit's.
def cluster_stopping_op(
    db: Database,
    machine: str,
    home: str,
    maintenance_for_seconds: int | None = None,
    maintenance_reason: str | None = None,
) -> dict[str, str]:
    mark_stopping(
        db,
        machine,
        home,
        maintenance_for_seconds=maintenance_for_seconds,
        maintenance_reason=maintenance_reason,
    )
    return {"machine": machine}


# meiva:note
# Local snapshot — assembled by `status_snapshot()`.
def cluster_status_op(
    db: Database,
    pool: ConnectionPool | None = None,
    *,
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
) -> ClusterStatus:
    return status_snapshot(db, pool=pool, image=image, local_machine=local_machine)


# meiva:note
# The runner-side half of `GET /api/cluster/machines/{name}/harnesses`: the gateway
# dispatches this op to the machine an agent would be born on, because an install
# directory and a login live on that box, not on the gateway's. `reason` passes
# through unchanged — the probe already separates not installed, installed outside
# PATH, not signed in and sign-in state unknown, and that sentence is what the form
# shows. The probe asks each CLI with a bounded timeout (`claude auth status`,
# `codex login status`, `agy models` — the last one fetches the catalogue, measured
# 2-5s) and never starts a session or spends model quota; it runs on the daemon's
# worker thread because a slow CLI, like a stalled network mount, would block the
# event loop that serves spawn and lifecycle. The gateway caches one answer per
# machine for 30 seconds, so a repeat read of the form does not reach this op.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def harness_probe_op(machine: str) -> HarnessProbeResult:
    return HarnessProbeResult(
        machine=machine,
        harnesses=[
            HarnessProbeRow(
                harness=row.harness,
                status=row.status,
                reason=row.reason,
                binary=row.binary,
                on_path=row.on_path,
            )
            for row in probe_local_harnesses()
        ],
    )


# meiva:note
# This host's live persistent-shell sessions for one agent.
#
# The runner-side half of the inspector panel's `shells` list: the gateway dispatches this op
# when the agent runs on this machine rather than on the gateway's own box
# (`agent_shell_sessions` is host-scoped, so a local probe on the gateway would always read empty
# for a remote agent). The listing's times render in the cluster's zone, so the instant is read
# here, at the op's entry.
def shell_probe_op(agent_id: int) -> ShellProbeResult:
    return ShellProbeResult(shells=agent_shell_sessions(agent_id, now=cluster_now()))


# meiva:note
# Kill one persistent shell on this runner for TTL reclamation.
#
# ``interrupted`` reports whether the kill cut short a running job — the gateway notifies the
# owner only then (an empty shell's reaping is silent).
def shell_kill_op(agent_id: int, session_id: int) -> ShellKillResult:
    mode, interrupted, name = kill_shell(agent_id, session_id)
    if mode is ShellKillMode.ABSENT:
        return ShellKillResult(mode=mode)
    return ShellKillResult(mode=mode, interrupted=interrupted, name=name)


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def _is_saver_conn(value: object) -> TypeGuard[Connection[DictRow]]:
    return isinstance(value, Connection)


# meiva:note
# The persisted cwd and effective skill-index narrowing for one agent.
#
# The daemon's shared pool keeps this read on the agent's machine. ``cwd`` is the meiva-code
# plugin's private channel key, following ``PluginStateHandle``'s ``<plugin>__<field>`` convention
# in ``agent/state.py``. An old agent with no checkpoint has no project-local roots; an old row
# with no frozen/overlay value falls through to the normal unfiltered (``["*"]``) command view.
def agent_skill_view_inputs(
    pool: ConnectionPool, agent_id: int
) -> tuple[Path | None, list[str] | None, str]:
    from langgraph.checkpoint.postgres import PostgresSaver

    from base.agents.history.checkpoint_serializer import CheckpointSerializer

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, birth_config, harness FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None, None, "meiva"
        overlay = row[0] if _is_str_dict(row[0]) else {}
        birth = row[1] if _is_str_dict(row[1]) else {}
        harness = str(row[2]) if len(row) > 2 and row[2] is not None else "meiva"
        pins = resolve_agent_config_pins(overlay, birth)
        wanted = pins.get("skills_to_inject_into_system_prompt")
        if not _is_saver_conn(conn):
            raise TypeError(f"expected psycopg Connection, got {type(conn).__name__}")

        saver = PostgresSaver(
            conn=conn,
            serde=CheckpointSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES),
        )
        checkpoint = saver.get({"configurable": {"thread_id": str(agent_id)}})

    cwd = checkpoint["channel_values"].get("meiva_code__cwd") if checkpoint else None
    narrowed = wanted if _is_str_list(wanted) else None
    return (Path(cwd) if isinstance(cwd, str) else None), narrowed, harness


def _project_skill_roots(cwd: Path | None) -> list[Path]:
    return [] if cwd is None else project_skill_roots(cwd)


# meiva:note
# Keep explicit commands plus skill commands selected as prompt capabilities.
#
# This intentionally mirrors ``agent.graph.prompt.capabilities.resolve_prompt_skills``: ``*``
# selects all loaded skills; otherwise a configured value matches the dotted identifier first and
# then the bare frontmatter name under the common dash/underscore fold. Only skill-as-command
# entries are narrowed; explicit command files remain available to every agent as they are not
# capabilities.
def _narrow_commands(
    commands: list[Command], wanted: list[str] | None, loaded: list[Skill]
) -> list[Command]:
    if wanted is None or "*" in wanted:
        return commands

    from base.packages.skills.names import match_key
    from base.packages.skills.tree import identifier, target

    by_ident = {match_key(identifier(skill)): skill for skill in loaded}
    by_name = {match_key(skill["name"]): skill for skill in loaded}
    selected_targets = {
        target(skill)
        for name in wanted
        if (skill := by_ident.get(match_key(name)) or by_name.get(match_key(name))) is not None
    }
    return [
        command
        for command in commands
        if command["skill_target"] is None or command["skill_target"] in selected_targets
    ]


# meiva:note
# Build the command-autocomplete view that ``agent_id`` sees on this host.
#
# Converged skills are discovered on the target runner, with the agent's checkpointed cwd
# contributing project-local roots only for this call. The provider is scoped to this call (and
# only it is taken back), so one request cannot leak its project skills into a later agent's
# result. The result also carries this runner's enabled MCP names as phase-2 groundwork.
def agent_skill_view_op(agent_id: int, pool: ConnectionPool) -> AgentSkillViewResult:
    from base.packages.composer_commands import CommandCatalog, load_skill_names
    from base.packages.mcp_config import load_mcp_config
    from base.packages.plugins.mcp_enabled import read_enabled

    cwd, wanted, harness = agent_skill_view_inputs(pool, agent_id)
    loaded = load_skill_names(_project_skill_roots(cwd))
    commands = _narrow_commands(
        CommandCatalog.system(skills=lambda: loaded).discover(), wanted, loaded
    )
    if harness != "meiva":
        commands = [c for c in commands if c["name"] != "compact"]
    merged_mcp = load_mcp_config(include_disabled=True)
    mcp_overlay = read_enabled()
    return AgentSkillViewResult(
        commands=[
            OpsCommandItem(
                name=command["name"],
                description=command["description"],
                instruction_hint=command["instruction_hint"],
            )
            for command in commands
        ],
        mcp_names=sorted(name for name in merged_mcp if mcp_overlay.get(name, True)),
    )


# meiva:note
# Capture one of an agent's persistent shells' terminal tail, locally.
#
# The runner-side half of the shell-monitor endpoint (`capture_shell` — resolves the session
# against this host's pty sessions and captures its tail). The gateway dispatches this op when the
# agent runs on this machine; `capture_shell` raises ShellNotFoundError / RuntimeError when the
# session is absent or died mid-capture, which the ops daemon surfaces as a 'failed' op result.
#
# The `lines` default is a direct-call fallback: the gateway resolves the configured default
# (display.shell_capture_default_lines) before dispatching, and every programmatic caller passes
# an explicit window.
#
# Raises:
#     ShellNotFoundError: no live shell with `session_id` on this host.
#     RuntimeError: the session capture failed.
def shell_capture_op(agent_id: int, session_id: int, lines: int = 200) -> ShellCaptureResult:
    full_name, captured, created_at, uptime_seconds = capture_shell(
        agent_id, session_id, lines, now=cluster_now()
    )
    return ShellCaptureResult(
        session_name=full_name,
        lines=captured,
        created_at=created_at,
        uptime_seconds=uptime_seconds,
    )

# meiva:note
# Launch plumbing both coding-tool launchers share: workspace paths, the facts a launch reads from the process, a new owner generation, owner-record status and cancel.
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from base.api_contracts.agent_contract import AgentNotFound, AgentStatus, GatewayUnavailable
from base.clock import SYSTEM_TIMERS, Timers
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.log import logger
from base.paths import meiva_home
from base.paths.expand import expand_home
from base.sessions import coding_session_owner, tool_capabilities
from base.sessions import provenance as provenance_block
from base.sessions.backend import get_shell_backend
from base.sessions.coding_session_owner_record import CodingSessionStatus
from base.sessions.protocol import SessionBackend
from meiva import agents
from meiva.files import agent_workspace
from meiva.sdk_surface import agent_identity, process_context
from meiva.shell import sessions
from meiva.shell.coding_tools.plans.lineage import DispatchFacts
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions

# meiva:note
# The lines asked of a capture whose screen alone matters: the service ignores them when `scrollback=False`.
VISIBLE_SCREEN_LINES = 200


# meiva:note
# A session that ended: `reason` is what the refusal said.
@dataclass(frozen=True)
class SessionGone:
    reason: str


# meiva:note
# The visible screen of a session, or `SessionGone` when it has ended. A session that ends while it is being read answers with the backend's own failure instead of the id refusal, so a failure of the backend counts as the session's end only when the session is no longer listed; otherwise it is the backend's failure and propagates.
def capture_screen(shell: ShellSessions, session_id: int) -> str | SessionGone:
    try:
        return shell.capture(session_id, VISIBLE_SCREEN_LINES, scrollback=False)
    except ValueError as exc:
        return SessionGone(str(exc))
    except RuntimeError as exc:
        if session_id in shell.list():
            raise
        return SessionGone(str(exc))


# meiva:note
# What a launch reads from the process, once: the calling agent's shell sessions, the home whose Claude and Codex files it touches, the cluster home its owner records live under, the directory a new session starts in (the agent's workspace), the machine facts a new session starts from, and the clocks one launch measures against — the timers its readiness polls spend and the instant it stamps a first run with.
#
# `timers` and `now` are here because a launch is the thing that owns a wait: every poll a launch runs (MCP startup, `/status`, the submission check, the shared app server) is timed by `timers`, and a first-run preset is stamped from `now`. Handing them out with the rest of the site keeps one reading of the process clock in this module, which is already where a launch reads its process facts; nothing the launcher calls reads a clock for itself.
@dataclass(frozen=True)
class LaunchSite:
    shell: ShellSessions
    home: Path
    cluster: Path
    session_cwd: Path
    host: SessionHost
    timers: Timers
    now: datetime

    def open_session(self, name: str, ttl: float) -> int:
        session_id, _full = self.shell.create(name, cwd=self.session_cwd, ttl=ttl, host=self.host)
        return session_id


# meiva:note
# The launch site of this process. It raises when the process has no agent identity: a launch is an agent's.
def launch_site() -> LaunchSite:
    shell = sessions.shell_sessions()
    return LaunchSite(
        shell=shell,
        home=Path.home(),
        cluster=meiva_home(),
        session_cwd=agent_workspace(shell.agent_id),
        host=sessions.session_host(sessions.process_environ()),
        timers=SYSTEM_TIMERS,
        now=datetime.now(UTC),
    )


# meiva:note
# The Meiva home of the bound context: the cluster whose tool-probe cache and owner records a status,
# a cancel or a launch of this process reads and writes.
def cluster_home() -> Path:
    return process_context.current_context().require_meiva_home()


# meiva:note
# The home `~` in an operator's path names, read once from the bound context: a workspace, a task
# file or a cluster directory a command was given.
def user_home() -> Path:
    return process_context.current_context().require_user_home()


# meiva:note
# The shell sessions of that cluster, for a command that has no agent shell of its own (a cancel).
def cluster_sessions() -> SessionBackend:
    return get_shell_backend(home=cluster_home())


# meiva:note
# A path a person typed, with its leading `~` expanded against `user_home` — the home the entry read
# and handed down, never this process's own. A `~someone/...` spelling names another account's home,
# which the invocation did not read, so it is refused on stderr like a missing workspace: one line,
# exit 1, rather than a traceback out of a launcher script.
def placed_path(raw: str, *, user_home: Path) -> Path:
    try:
        return expand_home(raw, user_home=user_home)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def resolve_dir(dir_path: str, *, user_home: Path) -> Path:
    """The workspace directory, resolved; exit 1 when it does not exist."""
    path = placed_path(dir_path, user_home=user_home).resolve()
    if not path.is_dir():
        print(f"error: {path} is not a directory or does not exist", file=sys.stderr)
        raise SystemExit(1)
    return path


def resolve_file(workspace: Path, raw: str, *, user_home: Path) -> Path:
    """Absolute path stays as given; a relative one is taken against the workspace."""
    path = placed_path(raw, user_home=user_home)
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def init_file(path: Path, initial: str) -> None:
    """Create the file with ``initial`` if absent, including parent directories."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(initial, encoding="utf-8")


# meiva:note
# Only the agent directory is queried, through the same gateway client the launch already uses for owner liveness; a failure there degrades the block to less lineage instead of blocking the launch. A lookup that failed is None in the facts. Without an agent identity there is nothing to look up.
def read_dispatch_facts() -> DispatchFacts:
    parent_id: int | None = None
    try:
        parent_id = agent_identity.require_agent_id()
    except Exception:
        logger.opt(exception=True).warning("dispatch provenance: no agent identity")
    if parent_id is None:
        return DispatchFacts(parent_agent_id=None, ancestor_ids=None, directory=None)
    ancestor_ids: tuple[int, ...] | None = None
    try:
        ancestor_ids = tuple(neighbor.agent_id for neighbor in agents.get_ancestors(parent_id))
    except Exception:
        logger.opt(exception=True).warning("dispatch provenance: ancestor lookup failed")
    directory: tuple[tuple[int, str | None], ...] | None = None
    try:
        page = agents.list_agents(scope="all", query=str(parent_id), limit=5)
        directory = tuple((row.agent_id, row.label) for row in page.agents)
    except Exception:
        logger.opt(exception=True).warning("dispatch provenance: agent lookup failed")
    return DispatchFacts(parent_agent_id=parent_id, ancestor_ids=ancestor_ids, directory=directory)


# meiva:note
# The task file belongs to the operator, so an existing block is never rewritten and the operator's
# text keeps its order below the generated one. A file that already carries the marker is left
# alone, which makes a relaunch of the same workspace idempotent.
def write_task_file_header(tasks_file: Path, block: str) -> None:
    if not block:
        return
    try:
        existing = tasks_file.read_text(encoding="utf-8") if tasks_file.exists() else ""
    except OSError:
        logger.opt(exception=True).warning(
            "dispatch provenance: cannot read {}; leaving the task file alone", tasks_file
        )
        return
    if provenance_block.has_marker(existing):
        return
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(f"{block}\n\n{existing}", encoding="utf-8")


# meiva:note
# One line for the operator and for the launch log: what this machine's CLI is, which declared
# features it has, and which path the launcher will therefore use. A tool with no declared
# capabilities reports nothing rather than inventing a version string.
def capability_report(tool: str, *, mode: str = "auto", force: bool = False) -> tuple[str, int]:
    spec = tool_capabilities.TOOLS.get(tool)
    if spec is None:
        return "", 0
    context = process_context.current_context()
    probe = tool_capabilities.probe_tool(
        spec,
        store=tool_capabilities.cache_path(context.require_meiva_home()),
        now=context.require_clock().now(),
        user_home=context.require_user_home(),
    )
    decision = tool_capabilities.decide(spec, requested=mode, force=force, probe=probe)
    return tool_capabilities.render(tool, decision, probe), 2 if decision.refused else 0


def owner_terminated(agent_id: int) -> bool:
    try:
        return agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except GatewayUnavailable:
        # An unavailable gateway cannot prove an owner dead. The supervisor and
        # expiry retain responsibility; guessing here could kill active work.
        logger.opt(exception=True).warning(
            "gateway unavailable while checking whether owner agent {} terminated; "
            "treating the owner as alive",
            agent_id,
        )
        return False


def kill_session_after_failed_launch(shell: ShellSessions, session_id: int) -> None:
    """Reclaim the PTY of a launch that is already failing; the launch's own error stays primary."""
    try:
        shell.kill(session_id)
    except Exception:
        logger.opt(exception=True).warning(
            "reclaiming shell session {} after a failed coding-session launch failed; "
            "it stays until its TTL expires",
            session_id,
        )


def terminate_generation_after_failed_launch(
    key: coding_session_owner.CodingSessionKey,
    generation: str,
    *,
    sessions: coding_session_owner.CodingSessions,
) -> None:
    """Roll back the record of a launch that is already failing; the launch's own error stays primary."""
    try:
        coding_session_owner.terminate_generation(
            key,
            generation,
            reason="launch-failed",
            writer=AtomicWriter(SYSTEM_FS),
            now=process_context.current_context().require_clock().now(),
            sessions=sessions,
        )
    except Exception:
        logger.opt(exception=True).warning(
            "rolling back coding-session generation {} after a failed launch failed; "
            "its record stays until the sweep reclaims it",
            generation,
        )


def new_generation(
    key: coding_session_owner.CodingSessionKey,
    *,
    owner_agent_id: int,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    sessions: coding_session_owner.CodingSessions,
) -> coding_session_owner.CodingSessionOwner:
    """A fresh generation of our own; dead siblings under ``key`` are reclaimed first."""
    context = process_context.current_context()
    return coding_session_owner.launch_generation(
        key,
        owner_agent_id=owner_agent_id,
        tasks_file=(None if tasks_file is None else expand_home(tasks_file, user_home=user_home())),
        work_file=(None if work_file is None else expand_home(work_file, user_home=user_home())),
        ttl_seconds=ttl_seconds,
        writer=AtomicWriter(SYSTEM_FS),
        now=context.require_clock().now(),
        rng=context.require_rng(),
        sessions=sessions,
        owner_terminated=owner_terminated,
    )


# meiva:note
# The fields of an owner record each tool's `--status` and `--cancel` print, in order, after `status=`.
_OWNER_FIELDS: dict[str, tuple[str, ...]] = {
    "claude": (
        "generation",
        "owner_agent_id",
        "session_id",
        "session_name",
        "state_dir",
        "tasks_file",
        "work_file",
    ),
    "codex": (
        "generation",
        "owner_agent_id",
        "session_id",
        "session_name",
        "supervisor_session_id",
        "supervisor_session_name",
        "tasks_file",
        "work_file",
    ),
}


def print_owner(owner: coding_session_owner.CodingSessionOwner, tool: str) -> None:
    print(f"status={owner.status}")
    for name in _OWNER_FIELDS[tool]:
        value = getattr(owner, name)
        if value is not None:
            print(f"{name}={value}")


def cancel(
    key: coding_session_owner.CodingSessionKey,
    generation: str,
    *,
    sessions: coding_session_owner.CodingSessions,
) -> int:
    """Stop and terminalize exactly ``generation``; a stale token is refused."""
    stopped = coding_session_owner.terminate_generation(
        key,
        generation,
        reason="explicit-cancel",
        writer=AtomicWriter(SYSTEM_FS),
        now=process_context.current_context().require_clock().now(),
        sessions=sessions,
    )
    if not stopped:
        print(f"cancel refused: no generation {generation} is recorded here", file=sys.stderr)
        return 1
    print_owner(coding_session_owner.read(key, generation), key.tool)
    return 0


# meiva:note
# `--status` answers what the next launch would do, so it reports the capability decision the same
# execution-path request would get (`capability`: the report line and its exit code): a refusal has to
# be visible in the preview and in its exit code, not only once an operator has already asked for the
# launch.
def status(key: coding_session_owner.CodingSessionKey, *, capability: tuple[str, int]) -> int:
    """Print every generation recorded for the workspace; exit 1 when one is invalid."""
    owners = coding_session_owner.list_generations(key)
    if not owners:
        print("status=inactive")
    for index, owner in enumerate(owners):
        if index:
            print()
        print_owner(owner, key.tool)
        if owner.error:
            print(f"error={owner.error}", file=sys.stderr)
    line, refusal = capability
    if line:
        print(line)
    if refusal:
        return refusal
    return 1 if any(owner.status == CodingSessionStatus.INVALID for owner in owners) else 0

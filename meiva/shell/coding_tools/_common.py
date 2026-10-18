"""Launch plumbing both coding-tool launchers share: workspace paths, a new
owner generation, owner-record status and cancel."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from base.api_contracts.agent_contract import AgentNotFound, AgentStatus, GatewayUnavailable
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.log import logger
from base.sessions import coding_session_owner, tool_capabilities
from base.sessions import provenance as provenance_block
from base.sessions.coding_session_owner_record import CodingSessionStatus
from meiva import agents
from meiva.sdk_surface import agent_identity
from meiva.shell import sessions


def resolve_dir(dir_path: str) -> Path:
    """The workspace directory, resolved; exit 1 when it does not exist."""
    path = Path(dir_path).expanduser().resolve()
    if not path.is_dir():
        print(f"error: {path} is not a directory or does not exist", file=sys.stderr)
        raise SystemExit(1)
    return path


def resolve_file(workspace: Path, raw: str) -> Path:
    """Absolute path stays as given; a relative one is taken against the workspace."""
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def session_uuid(value: str) -> str:
    """Parse a ``--resume`` argument: the tool session id a launch printed (a UUID)."""
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a session id; pass the UUID the launch printed"
        ) from exc


def init_file(path: Path, initial: str) -> None:
    """Create the file with ``initial`` if absent, including parent directories."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(initial, encoding="utf-8")


# meiva:note
# An already-rendered dispatch block leads the message, so the worker reads who dispatched it before
# the contract and the two file paths. A takeover never reaches this function: its briefing carries
# the three-line form and creates no files.
def worker_bootstrap(
    contract: Path,
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
    *,
    resumed: bool = False,
    provenance: str | None = None,
) -> str:
    """The supervised worker's first message: the contract and both file paths.

    A resumed session (``resumed``) is told it was interrupted and continues
    from its own history instead of starting over.
    """
    interrupted = (
        "This session was interrupted (its shell was closed) and has been resumed with "
        "its history. "
        if resumed
        else ""
    )
    return (f"{provenance}\n\n" if provenance else "") + (
        f"{interrupted}Read the collaboration contract at {contract} and follow it. "
        f"Your workspace is {workspace}. "
        f"Your task file (read-only for you) is {tasks_file}. "
        f"Your work file (yours to write, STATUS + log) is {work_file}. "
        + (
            "Re-read the task file and your work file, then continue from where you stopped."
            if resumed
            else "Now read the task file and start working."
        )
    )


# meiva:note
# Only the agent directory is queried, through the same gateway client the launch already uses for
# owner liveness; a failure there degrades the block to an unknown dispatcher instead of blocking
# the launch. The three sources are parameters so a test passes its own instead of patching the SDK.
def build_dispatch_provenance(
    *,
    for_task: int | None,
    why: str,
    task_label: str | None = None,
    priority: str | None = None,
    now: dt.datetime | None = None,
    agent_id: Callable[[], int] | None = None,
    ancestors: Callable[[int], list[agents.Neighbor]] | None = None,
    directory: Callable[..., agents.AgentDirectoryPage] | None = None,
) -> provenance_block.DispatchProvenance:
    read_agent_id = agent_id or agent_identity.require_agent_id
    read_ancestors = ancestors or agents.get_ancestors
    read_directory = directory or agents.list_agents
    parent_id: int | None = None
    try:
        parent_id = read_agent_id()
    except Exception:
        logger.opt(exception=True).warning("dispatch provenance: no agent identity")

    chain: tuple[str, ...] = ()
    label: str | None = None
    if parent_id is not None:
        chain = _spawn_chain(parent_id, read_ancestors)
        label = _agent_label(parent_id, read_directory)
    return provenance_block.DispatchProvenance(
        parent_agent_id=parent_id,
        parent_label=label,
        spawn_chain=chain,
        dispatched_at=now or dt.datetime.now(dt.UTC),
        why=why,
        task_id=for_task,
        task_label=task_label,
        priority=priority,
    )


def _spawn_chain(
    agent_id: int, read_ancestors: Callable[[int], list[agents.Neighbor]]
) -> tuple[str, ...]:
    try:
        ancestors = read_ancestors(agent_id)
    except Exception:
        logger.opt(exception=True).warning("dispatch provenance: ancestor lookup failed")
        return ()
    chain = [*(str(neighbor.agent_id) for neighbor in reversed(ancestors)), str(agent_id)]
    return tuple(chain[-5:])


# meiva:note
# A missing label is not an error: the directory may not know the agent yet, and the block still
# names it by id. Only the id is load-bearing.
def _agent_label(
    agent_id: int, read_directory: Callable[..., agents.AgentDirectoryPage]
) -> str | None:
    try:
        page = read_directory(scope="all", query=str(agent_id), limit=5)
    except Exception:
        logger.opt(exception=True).warning("dispatch provenance: agent lookup failed")
        return None
    for row in page.agents:
        if row.agent_id == agent_id:
            return row.label
    return None


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
    probe = tool_capabilities.probe_tool(spec)
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


def kill_session_after_failed_launch(session_id: int) -> None:
    """Reclaim the PTY of a launch that is already failing; the launch's own error stays primary."""
    try:
        sessions.kill(session_id)
    except Exception:
        logger.opt(exception=True).warning(
            "reclaiming shell session {} after a failed coding-session launch failed; "
            "it stays until its TTL expires",
            session_id,
        )


def terminate_generation_after_failed_launch(
    key: coding_session_owner.CodingSessionKey, generation: str
) -> None:
    """Roll back the record of a launch that is already failing; the launch's own error stays primary."""
    try:
        coding_session_owner.terminate_generation(
            key, generation, reason="launch-failed", writer=AtomicWriter(SYSTEM_FS)
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
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
) -> coding_session_owner.CodingSessionOwner:
    """A fresh generation of our own; dead siblings under ``key`` are reclaimed first."""
    return coding_session_owner.launch_generation(
        key,
        owner_agent_id=agent_identity.require_agent_id(),
        tasks_file=tasks_file,
        work_file=work_file,
        ttl_seconds=ttl_seconds,
        writer=AtomicWriter(SYSTEM_FS),
        owner_terminated=owner_terminated,
    )


OwnerPrinter = Callable[[coding_session_owner.CodingSessionOwner], None]


def cancel(
    key: coding_session_owner.CodingSessionKey, generation: str, print_owner: OwnerPrinter
) -> int:
    """Stop and terminalize exactly ``generation``; a stale token is refused."""
    stopped = coding_session_owner.terminate_generation(
        key, generation, reason="explicit-cancel", writer=AtomicWriter(SYSTEM_FS)
    )
    if not stopped:
        print(f"cancel refused: no generation {generation} is recorded here", file=sys.stderr)
        return 1
    print_owner(coding_session_owner.read(key, generation))
    return 0


# meiva:note
# `--status` answers what the next launch would do, so it forwards the same execution-path request the
# launch would receive: a refusal has to be visible in the preview and in its exit code, not only
# once an operator has already asked for the launch.
def status(
    key: coding_session_owner.CodingSessionKey,
    print_owner: OwnerPrinter,
    *,
    mode: str = "auto",
    force: bool = False,
    report: Callable[..., tuple[str, int]] = capability_report,
) -> int:
    """Print every generation recorded for the workspace; exit 1 when one is invalid."""
    owners = coding_session_owner.list_generations(key)
    if not owners:
        print("status=inactive")
    for index, owner in enumerate(owners):
        if index:
            print()
        print_owner(owner)
        if owner.error:
            print(f"error={owner.error}", file=sys.stderr)
    line, refusal = report(key.tool, mode=mode, force=force)
    if line:
        print(line)
    if refusal:
        return refusal
    return 1 if any(owner.status == CodingSessionStatus.INVALID for owner in owners) else 0

from __future__ import annotations

from typing import Literal

import meiva.sdk_surface.agent_identity
from base.api_contracts.agent_contract import AgentLaunchFailed as AgentLaunchFailed

# Redundant-alias re-exports: importable from this module but deliberately not
# in __all_for_meiva__ — error types never render into the SDK docs every agent carries;
# a traceback names them clearly on the rare occasion one fires. The aliases keep
# pyright from flagging them unused.
from base.api_contracts.agent_contract import AgentNotFound as AgentNotFound
from base.api_contracts.agent_contract import (
    AgentStatus,
    CancelResult,
    Harness,
    RestartResult,
    ResurrectResult,
    TerminateResult,
)
from base.api_contracts.agent_contract import (
    CrossMachineGatewayUnavailable as CrossMachineGatewayUnavailable,
)
from base.api_contracts.agent_contract import ForkCheckpointNotFound as ForkCheckpointNotFound
from base.api_contracts.agent_contract import (
    ForkConfigChangeNotAllowed as ForkConfigChangeNotAllowed,
)
from base.api_contracts.agent_contract import ForkError as ForkError
from base.api_contracts.agent_contract import ForkHarnessMismatch as ForkHarnessMismatch
from base.api_contracts.agent_contract import ForkSourceEmpty as ForkSourceEmpty
from base.api_contracts.agent_contract import GatewayUnavailable as GatewayUnavailable
from base.api_contracts.agent_contract import HarnessNotLaunchable as HarnessNotLaunchable
from base.api_contracts.agent_contract import HarnessStatus as HarnessStatus
from base.api_contracts.agent_contract import InvalidModelConfig as InvalidModelConfig
from base.api_contracts.agent_contract import MachineNotRegistered as MachineNotRegistered
from base.api_contracts.agent_contract import ProbedHarnessModel as ProbedHarnessModel
from base.api_contracts.agent_contract import ResurrectError as ResurrectError
from base.api_contracts.agent_contract import (
    ShellSessionKillTiming as ShellSessionKillTiming,
)
from base.api_contracts.agent_contract import SpawnTargetNotAgentRunner as SpawnTargetNotAgentRunner
from base.host.boundary.json_values import is_object_dict
from meiva import gateway_client
from meiva.agents import peers, schedule_capability
from meiva.agents.contracts import (
    CommandInfo,
    HarnessAvailability,
    Machine,
    Neighbor,
    ShellSessionsKill,
)
from meiva.agents.gateway_rows import (
    AgentDirectoryPage as AgentDirectoryPage,
)
from meiva.agents.gateway_rows import (
    AgentRow as AgentRow,
)
from meiva.agents.gateway_rows import (
    OpenTaskRow as OpenTaskRow,
)
from meiva.agents.gateway_rows import (
    OpenTasksHint as OpenTasksHint,
)
from meiva.agents.gateway_rows import (
    machines_from_rows,
    neighbor_from_row,
    object_rows,
    open_tasks_from_dict,
    optional_integer,
    row_from_dict,
    shell_sessions_from_dict,
    text,
)
from meiva.sdk_surface import process_context
from meiva.sdk_surface.validation import coerce_str, coerce_typed

__all_for_meiva__ = [
    "AgentDirectoryPage",
    "AgentRow",
    "AgentStatus",
    "CancelResult",
    "CommandInfo",
    "Harness",
    "HarnessAvailability",
    "Machine",
    "Neighbor",
    "OpenTaskRow",
    "OpenTasksHint",
    "ProbedHarnessModel",
    "RestartResult",
    "ResurrectResult",
    "ShellSessionKillTiming",
    "ShellSessionsKill",
    "TerminateOutcome",
    "TerminateResult",
    "cancel",
    "commands",
    "compact",
    "get_ancestors",
    "get_last_message",
    "get_neighbors",
    "get_status",
    "harness_availability",
    "list_agents",
    "list_machines",
    "restart",
    "resurrect",
    "send_message",
    "spawn",
    "terminate",
]


class TerminateOutcome(str):
    """What a terminate call returned: the acceptance status as a string —
    compare it directly ("enqueued" / "already_terminated") — plus `status` as
    the enum, `open_tasks`: the tasks the agent still owned as it went down, or
    None, and `shell_sessions`: what `kill_all_shell_sessions` did, or None
    when no shell session was killed on request."""

    __slots__ = ("open_tasks", "shell_sessions", "status")

    status: TerminateResult
    open_tasks: OpenTasksHint | None
    shell_sessions: ShellSessionsKill | None

    def __new__(
        cls,
        status: TerminateResult,
        open_tasks: OpenTasksHint | None = None,
        *,
        shell_sessions: ShellSessionsKill | None = None,
    ) -> TerminateOutcome:
        self = super().__new__(cls, status.value)
        self.status = status
        self.open_tasks = open_tasks
        self.shell_sessions = shell_sessions
        return self

    def __repr__(self) -> str:
        return (
            f"TerminateOutcome(status={self.status.value!r}, "
            f"open_tasks={self.open_tasks!r}, shell_sessions={self.shell_sessions!r})"
        )


def get_neighbors(
    agent_id: int, depth: int | None = None, limit: int | None = None
) -> list[Neighbor]:
    """Rank the agents most strongly tied to `agent_id`.

    Ties form on spawn, fork, resurrect, or send_message and fade with time;
    `depth` is how many hops out to look. Omit `depth`/`limit` for the
    configured defaults (``display.neighbors_default_depth`` /
    ``display.neighbors_default_limit`` - 1 / 20 out of the box).
    """
    from meiva.sdk_surface.settings import config_authority

    agent_id = coerce_typed(agent_id, "agent_id", int)
    resolved_depth: int
    if depth is None:
        resolved_depth = config_authority().service_field_value("neighbors_default_depth")
    else:
        resolved_depth = coerce_typed(depth, "depth", int)
    resolved_limit: int
    if limit is None:
        resolved_limit = config_authority().service_field_value("neighbors_default_limit")
    else:
        resolved_limit = coerce_typed(limit, "limit", int)
    rows = object_rows(
        gateway_client.get_neighbors(agent_id, depth=resolved_depth, limit=resolved_limit),
        "neighbors",
    )
    return [neighbor_from_row(row) for row in rows]


def get_ancestors(agent_id: int) -> list[Neighbor]:
    """The spawn/fork chain above `agent_id`, nearest ancestor first.

    Message ties never form ancestors, and an agent spawned by the user (or
    with no recorded spawn) returns [].
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    rows = object_rows(gateway_client.get_ancestors(agent_id), "ancestors")
    return [neighbor_from_row(row) for row in rows]


# meiva:note
# Wider than the `Literal` parameter on purpose: the model can pass any string, and the check below
# is the fail-fast boundary for it.
_DIRECTORY_SCOPES: tuple[str, ...] = ("live", "terminated", "all")


def list_agents(
    *,
    scope: Literal["live", "terminated", "all"] = "live",
    query: str = "",
    before_id: int | None = None,
    limit: int = 100,
) -> AgentDirectoryPage:
    """Read one page of agents, newest IDs first.

    `live` includes every agent that has not terminated. Search by label
    substring or exact ID with `query` (at most 200 characters);
    use `terminated` for history or `all` for both. The page holds at most
    `limit` agents (1 through 200). Continue explicitly with `next_cursor`
    as `before_id`, retaining the same scope and query.
    """
    requested_scope = coerce_str(scope, "scope")
    query = coerce_str(query, "query")
    if len(query) > 200:
        raise ValueError("query must be at most 200 characters")
    before_id = coerce_typed(before_id, "before_id", int, allow_none=True)
    limit = coerce_typed(limit, "limit", int)
    if requested_scope not in _DIRECTORY_SCOPES:
        raise ValueError("scope must be 'live', 'terminated', or 'all'")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if before_id is not None and not 1 <= before_id <= 9223372036854775807:
        raise ValueError("before_id must be a positive bigint agent ID")
    page = gateway_client.list_agents(
        scope=requested_scope, query=query, before_id=before_id, limit=limit
    )
    if not is_object_dict(page):
        raise TypeError(f"agents page must be an object, got {type(page).__name__}")
    return AgentDirectoryPage(
        agents=[row_from_dict(row) for row in object_rows(page.get("agents"), "agents")],
        next_cursor=optional_integer(page.get("next_cursor"), "next_cursor"),
    )


def list_machines() -> list[Machine]:
    return machines_from_rows(gateway_client.list_machines())


def harness_availability() -> list[HarnessAvailability]:
    """Report which harnesses this machine can run, one row per harness.

    Each row says whether the harness's command is installed and whether it is signed
    in, with a readable reason. A sign-in state that cannot be read here reports
    `unknown` instead of being guessed, and an unproven sign-in is never reported as a
    proven one. The check asks that harness's own CLI for its login state under a
    bounded timeout; it starts no session, spends no quota and reads this machine only.
    """
    from base.agents.birth.harness import probe_local_harnesses
    from meiva.sdk_surface import process_context
    from meiva.sdk_surface import settings as sdk_settings

    return [
        HarnessAvailability(
            harness=row.harness,
            status=row.status,
            reason=row.reason,
            binary=row.binary,
            on_path=row.on_path,
            models=row.models,
            default_model=row.default_model,
        )
        for row in probe_local_harnesses(
            now=sdk_settings.clock().now(),
            environ=process_context.current_context().require_environ(),
        )
    ]


def spawn(
    prompt: str | None = None,
    fork_from: int | None = None,
    machine: str | None = None,
    config_overlay: dict[str, object] | None = None,
    label: str | None = None,
    harness: str | Harness | None = None,
) -> int:
    """Start a new agent; does not block.

    The runtime identifies each creation or fork. A replay of the original
    execution recovers its accepted agent ID. Acceptance only records birth.

    `harness` selects the initial runtime loop. It defaults to "meiva" and also
    accepts "claude_code", "codex" or "antigravity". Unknown names are refused.
    Use `restart` to switch harnesses later. Forking (`fork_from`) is only
    supported for native "meiva" agents, and asking a fork for an external harness or forking an
    external harness agent fails with `ForkHarnessMismatch`.
    `harness_availability()` reports which ones this machine can run. A harness no launcher
    can start yet is refused with `HarnessNotLaunchable` before any row is written, and so is
    a fork of an agent born on one.

    `prompt` is the first message — make it self-contained; omit it to leave the agent idling.
    `label` names the new agent's role, which peers and the fleet view show; omit it to let the
    system name the agent. `machine` defaults to yours. `config_overlay` carries per-agent config
    fields; keys it does not name follow the shared default. Identity config you do not name
    (model, reasoning effort, the inject/expand lists, prompt shaping) is frozen at spawn;
    operational knobs follow the shared default. `fork_from` copies another agent's conversation
    and keeps its config: the overlay may then only add entries to
    `skills_to_inject_into_system_prompt` / `skills_to_expand_at_start` (anything else raises
    ForkConfigChangeNotAllowed).
    A fork taken while that agent is inside a tool call carries the call without its result: the
    new agent sees it as already interrupted, not as work still running, so it checks whether the
    call already took effect instead of repeating it.
    The copy is the whole conversation: the child carries it and pays its size on every call, and
    the parent's own history is unchanged.
    `{"eval_isolation": True, "eval_network_allowlist": ["web"]}` isolates the agent, allowing
    only the listed `web` / `understand`; `mcps` and `ui` are off.
    """
    return spawn_impl(
        prompt=prompt,
        fork_from=fork_from,
        machine=machine,
        config=config_overlay,
        label=label,
        harness=harness,
    )


def spawn_impl(
    *,
    prompt: str | None,
    fork_from: int | None,
    machine: str | None,
    config: dict[str, object] | None,
    label: str | None,
    harness: str | Harness | None,
) -> int:
    prompt = coerce_str(prompt, "prompt", allow_none=True)
    fork_from = coerce_typed(fork_from, "fork_from", int, allow_none=True)
    machine = coerce_str(machine, "machine", allow_none=True)
    config = coerce_typed(config, "config", dict, allow_none=True)
    label = coerce_str(label, "label", allow_none=True)
    from base.agents.birth.harness import coerce_harness

    coerced_harness = None if harness is None else coerce_harness(harness)
    if (
        fork_from is not None
        and coerced_harness is not None
        and coerced_harness is not Harness.MEIVA
    ):
        raise ForkHarnessMismatch(
            f"cannot fork agent {fork_from} onto harness {coerced_harness.value!r}: "
            f"forking is only supported on the native 'meiva' harness."
        )
    harness = None if coerced_harness is None else coerced_harness.value
    spawner = meiva.sdk_surface.agent_identity.require_actor()
    if config:
        from base.packages.plugins.config_registration import validate_config_overlay
        from meiva.sdk_surface import settings as sdk_settings
        from meiva.sdk_surface.installation import installed

        installation = installed()
        effective_harness = coerced_harness or Harness.MEIVA
        if effective_harness is Harness.MEIVA:
            catalog = sdk_settings.model_catalog()
            models_map = catalog.models
            decision_models = catalog.decision_models
            prices = catalog.prices
        else:
            models_map = None
            decision_models = (
                None
                if installation is None or installation.catalog is None
                else installation.catalog.decision_models
            )
            prices = None
        validate_config_overlay(
            config,
            None if installation is None else installation.configs,
            models=models_map,
            decision_models=decision_models,
            prices=prices,
            home=process_context.current_context().require_meiva_home(),
        )
    return gateway_client.spawn(
        spawner=spawner,
        prompt=prompt,
        fork_from=fork_from,
        prompt_source=spawner,
        machine=machine
        if machine is not None
        else process_context.current_context().require_machine().name(),
        config=config,
        label=label,
        harness=harness,
        idempotency_key=process_context.current_context().sdk_operation_uid(),
    )


def terminate(
    agent_id: int,
    *,
    message: str | None = None,
    force: bool = False,
    kill_all_shell_sessions: bool = False,
) -> TerminateOutcome:
    """End an agent after its current step.

    `message` is stored, without a reply, for when the agent is revived.
    `force=True` interrupts work; an `enqueued` result confirms acceptance, not
    exit. A terminated agent wakes on any new message, including from its own
    shell sessions or watchers; `kill_all_shell_sessions=True` also kills every
    shell session it owns, watchers included, silently. Pages opened with
    `meiva.ui.serve` keep running.

    The result equals its status string and carries `open_tasks` (up to five
    tasks it still owns, most recently updated first, or None) and
    `shell_sessions` (a `ShellSessionsKill`, or None when no kill ran)."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    schedule_capability.assert_schedule_capability("agents.terminate", target_agent_id=agent_id)
    message = coerce_str(message, "message", allow_none=True)
    force = coerce_typed(force, "force", bool)
    kill_all_shell_sessions = coerce_typed(kill_all_shell_sessions, "kill_all_shell_sessions", bool)
    data = gateway_client.terminate(
        agent_id,
        message=message,
        force=force,
        kill_all_shell_sessions=kill_all_shell_sessions,
    )
    if not is_object_dict(data):
        raise TypeError(f"terminate reply must be an object, got {type(data).__name__}")
    return TerminateOutcome(
        TerminateResult(text(data["status"], "status")),
        open_tasks_from_dict(data.get("open_tasks")),
        shell_sessions=shell_sessions_from_dict(data.get("shell_sessions")),
    )


def restart(
    agent_id: int, message: str, *, config_overlay: dict[str, object] | None = None
) -> RestartResult:
    """Restart an agent under the same id.

    `message` is a mandatory, non-blank explanation shown to the agent.
    `config_overlay` carries persistent configuration changes such as `llm_model`.
    Set its `harness` field to switch the runtime loop of an idling or terminated agent.
    An agent staying on "meiva" can restart and continue a turn in progress."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    schedule_capability.assert_schedule_capability("agents.restart", target_agent_id=agent_id)
    message = coerce_str(message, "message")
    if not message.strip():
        raise ValueError("message must not be blank")
    config_overlay = coerce_typed(config_overlay, "config_overlay", dict, allow_none=True)
    return RestartResult(
        gateway_client.restart(agent_id, message=message, config_overlay=config_overlay)
    )


def cancel(agent_id: int) -> CancelResult:
    """Cancel the agent's running turn; the agent stays alive and idles.

    `cancelled` means a native turn was cancelled. `enqueued` means an external
    harness has a foreground cancellation pending. `not_running` means there
    was no running turn."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    schedule_capability.assert_schedule_capability("agents.cancel", target_agent_id=agent_id)
    return CancelResult(gateway_client.cancel(agent_id))


def compact(agent_id: int) -> None:
    """Queue a compaction of the agent's history for its next turn.

    Returns once the request is durable; the summary is generated and applied
    by the agent itself, and no result is reported back."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    schedule_capability.assert_schedule_capability("agents.compact", target_agent_id=agent_id)
    gateway_client.compact(agent_id)


def resurrect(agent_id: int, prompt: str) -> ResurrectResult:
    """Wake a terminated agent on its current harness with its previous conversation state intact."""
    agent_id = coerce_typed(agent_id, "agent_id", int)
    schedule_capability.assert_schedule_capability("agents.resurrect", target_agent_id=agent_id)
    prompt = coerce_str(prompt, "prompt")
    return ResurrectResult(gateway_client.resurrect(agent_id, prompt=prompt))


def commands() -> list[CommandInfo]:
    """List the commands a peer agent accepts; invoke one by sending
    `/name <instruction>` as the message text."""
    from base.packages.composer_commands import CommandCatalog
    from meiva import skills

    return [
        CommandInfo(
            name=c["name"], description=c["description"], instruction_hint=c["instruction_hint"]
        )
        for c in CommandCatalog.system(
            skills=skills.names, home=process_context.current_context().require_meiva_home()
        ).discover()
    ]


def send_message(agent_id: int, content: str) -> None:
    """Does not wait or confirm delivery.

    A terminated target is auto-resurrected to handle the message.
    """  # lint-docstring: ok "auto-resurrected" is public behaviour, not impl detail
    agent_id = coerce_typed(agent_id, "agent_id", int)
    content = coerce_str(content, "content", allow_types=(list,))
    source = meiva.sdk_surface.agent_identity.require_actor()
    gateway_client.send_message(agent_id, content=content, source=source)


def send_system_note(
    agent_id: int,
    content: str,
    *,
    tag: str = "task",
    task_id: int | None = None,
    resurrect: bool = True,
    idempotency_key: str,
) -> int:
    """Deliver a framework system note to another agent.

    The note appears in the target agent's timeline as a system note (no
    sender prefix, no peer timestamp), not as a chat message from you. `tag`
    selects the note chip — a NoteTag value; `task` is the task-notification
    family (assign / update / reminder). `resurrect` revives a terminated
    target so it can receive the note: pass True only for real work
    directions (a task assignment), never for plain notifications (user
    ruling 2026-08-27).

    `task_id` links this note to a task in the timeline. It does not attribute
    the target's LLM usage to that task.

    `idempotency_key` identifies one logical send across your own retries.
    Reuse it only for the same note and policy; a new send needs a new key.

    Returns the durable inbound id. Does not wait for the target to act.
    """  # lint-docstring: ok "resurrect" is public behaviour, not impl detail
    note = peers.validate_system_note(
        agent_id,
        content,
        tag=tag,
        task_id=task_id,
        resurrect=resurrect,
        idempotency_key=idempotency_key,
    )
    source = meiva.sdk_surface.agent_identity.require_actor()
    return gateway_client.send_system_note(
        note.agent_id,
        content=note.content,
        note_tag=note.tag,
        source=source,
        task_id=note.task_id,
        resurrect=note.resurrect,
        idempotency_key=note.idempotency_key,
    )


def get_last_message(agent_id: int) -> str | None:
    """Return the agent's most recent AI turn text, or None when none yet.

    The text is the agent's latest output, not necessarily addressed to you:
    it may be text sent to another agent or an update for the user, and it is
    not a message inbox. To check whether the agent sent you a message, look
    at the messages you received, not this text. While the agent is working it
    can be a half-finished preamble that ends mid-sentence (often with a
    trailing colon), not a conclusion. The text is peer-authored, so it
    passes through the same prompt-injection scan as inbound chat messages."""
    from meiva.security import scan_content

    agent_id = coerce_typed(agent_id, "agent_id", int)
    caller = meiva.sdk_surface.agent_identity.require_actor()
    message = gateway_client.get_last_message(agent_id, caller)
    scan = peers.last_message_scan(message, agent_id)
    if scan is not None:
        scan_content(scan.text, source=scan.source)
    return message


def get_status(agent_id: int) -> AgentStatus:
    agent_id = coerce_typed(agent_id, "agent_id", int)
    return AgentStatus(gateway_client.get_agent(agent_id)["status"])

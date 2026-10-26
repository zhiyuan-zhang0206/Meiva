from __future__ import annotations

from pathlib import Path
from typing import LiteralString, NamedTuple, NoReturn, Protocol

from base.agents.lifecycle_signals import AgentRestart, AgentTermination, SystemHalt
from meiva.boot import maybe_load_plugins_for_missing
from meiva.files import workspace_base
from meiva.sdk_surface import attachment_transport, process_context
from meiva.sdk_surface import settings as _settings
from meiva.sdk_surface.help import HARNESS_HIDDEN_SELF_MEMBERS as HARNESS_HIDDEN_SELF_MEMBERS
from meiva.sdk_surface.validation import coerce_str, coerce_typed
from meiva.self import plans

HARNESS_SELF_MEMBERS: tuple[str, ...] = (
    "AGENT_ID",
    "MACHINE_SPEC",
    "MachineSpec",
    "pause_heartbeat",
    "terminate",
)

# Deliberately NOT in __all_for_meiva__ (importable, but out of the rendered SDK
# docs): AgentRestart / AgentTermination are framework control-flow exceptions
# raised on the success path of restart() / terminate() — the agent never
# catches them; InvalidConfigOverlay is a rare outcome whose traceback
# explains itself when one fires.
__all_for_meiva__ = [
    "AGENT_ID",
    "MACHINE_SPEC",
    "MachineSpec",
    "attach",
    "compact",
    "pause_heartbeat",
    "restart",
    "terminate",
]

# AGENT_ID is the agent-facing read of this process's identity. The canonical
# slot lives framework-internally in `meiva.sdk_surface.agent_identity` (set once by the bootstrap at
# process startup); this is a re-export served by the module `__getattr__`
# below, NOT a stored attribute — so the kernel never reaches through this
# disable-able `meiva.self` module to learn who it is, and `MEIVA_SDK_DISABLE` can
# remove the whole `self` namespace without stripping the identity.
#
# Annotation-only (no value binding) keeps the name out of the module dict so
# every `meiva.self.AGENT_ID` access routes to `__getattr__`.
AGENT_ID: int


class MachineSpec(NamedTuple):
    """Identity of a machine.

    Fields:
        name: the machine's name.
        description: free prose about what the machine is for; empty if unset.
    """

    name: str
    description: str


MACHINE_SPEC: MachineSpec
"""The machine from the bound context; no context is an error."""


def __getattr__(name: str) -> object:
    if name == "AGENT_ID":
        # Re-export the framework-internal identity as a plain int (the None
        # placeholder before bootstrap). `meiva.help(meiva.self)` still documents
        # the name from the AnnAssign + PEP 224 docstring above — a bare value
        # routes through the doc-carrying constant renderer there.
        import meiva.sdk_surface.agent_identity
        from meiva.self.harness import ensure_identity

        ensure_identity()
        return meiva.sdk_surface.agent_identity.agent_id()
    if name == "MACHINE_SPEC":
        machine = process_context.current_context().require_machine()
        return MachineSpec(machine.name(), machine.description() or "")
    if name == "InvalidConfigOverlay":
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay

        return InvalidConfigOverlay
    # Plugin members land on meiva.self via a declared `SdkMember` (meiva_fleet
    # adds set_label). In an agent-launched persistent-shell
    # child they are absent until plugins load, and this module already exists so
    # meiva.__getattr__ never fires — trigger the shared lazy load here, then retry.
    import sys as _sys

    if maybe_load_plugins_for_missing(name):
        return getattr(_sys.modules[__name__], name)
    raise AttributeError(f"module 'meiva.self' has no attribute {name!r}")


# `InvalidConfigOverlay` is lazily bound via module __getattr__ —
# any eager `from base.packages.plugins.config_registration import ...` in meiva.self triggers
# agent.__init__ → agent.graph.prompt._base_prompt calling `meiva.help(...)`
# which reverse-accesses an meiva attribute, while meiva.__init__ is still
# running line 91 `import meiva.self` and meiva.help isn't registered yet →
# AttributeError. Lazy makes this import chain only resolve when the
# caller actually accesses InvalidConfigOverlay.


class _Cursor(Protocol):
    def execute(self, query: LiteralString, params: tuple[object, ...] = ..., /) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


def _assert_native_self_action(action: str, cur: _Cursor | None = None) -> None:
    from meiva.sdk_surface import agent_identity
    from meiva.sdk_surface.pure.caller.identity_plan import native_harness_refusal

    ctx = process_context.ambient_context()
    if ctx is not None and ctx.harness.value != "meiva":
        refused = native_harness_refusal(ctx.harness.value, action)
        if refused is not None:
            raise RuntimeError(refused)
    if (
        ctx is not None
        and ctx.sdk_capture is not None
        and getattr(ctx.sdk_capture, "handle", None) is not None
    ):
        reg = getattr(ctx.sdk_capture, "_registration", None)
        harness_name = str(getattr(reg, "harness", None) or "external")
        refused = native_harness_refusal(harness_name, action)
        if refused is not None:
            raise RuntimeError(refused)
    if cur is not None:
        agent_id = agent_identity.agent_id()
        if agent_id is not None:
            cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if row is not None and row[0] != "meiva":
                refused = native_harness_refusal(str(row[0]), action)
                if refused is not None:
                    raise RuntimeError(refused)


def attach(path: str | Path, *, label: str | None = None) -> None:
    """Register a local media file for your next turn.

    Use this when your next response needs to inspect a file generated during
    this turn. The file is read when this turn ends, so keep it available until
    then. Your model receives supported media natively; a file whose modality
    your model cannot receive (e.g. video on an image-only model) is rejected
    with an error naming the allowed set. Attach at most 8 files and 48 MiB per
    turn, with a 20 MiB limit per file. Raises an error outside an agent turn.
    """
    _assert_native_self_action("attach")
    attachment_transport.register(path, label=label, base=workspace_base())


def _publish_self_inbound_wake() -> None:
    """Best-effort wake for a self-inserted inbound (restart / terminate / compact), over
    the agent's own SDK redis handle (`meiva.context.redis`); see `meiva.self.wake`."""
    from meiva.sdk_surface import agent_identity
    from meiva.self import wake

    wake.publish_inbound_wake(
        process_context.current_context().redis,
        agent_identity.require_agent_id(),
        events_channel=_settings.settings.data_plane.events_channel,
    )


def _run(cur: _Cursor, statements: tuple[plans.SqlStatement, ...]) -> None:
    for statement in statements:
        cur.execute(statement.sql, statement.params)


def restart(config_overlay: dict[str, object] | None = None) -> NoReturn:
    """Restart yourself under the same identity, preserving your conversation.

    This call ends the current execution; finish required work before calling it.
    `config_overlay` is a flat `{field_name: value}` mapping merged into your
    persistent per-agent settings."""
    config_overlay = coerce_typed(config_overlay, "config_overlay", dict, allow_none=True)
    from meiva.sdk_surface import agent_identity

    _assert_native_self_action("restart")
    agent_identity.assert_self_action("restart")
    if config_overlay:
        from base.packages.plugins.config_registration import validate_config_overlay
        from meiva.sdk_surface.installation import installed

        installation = installed()
        catalog = _settings.model_catalog()
        validate_config_overlay(
            config_overlay,
            None if installation is None else installation.configs,
            models=catalog.models,
            decision_models=catalog.decision_models,
        )
        # Settle a withdrawn llm_model before it is stored (task #4306): the
        # rewrite is reported in this agent's own log — the spawner-visible
        # receipt for the self-restart path.
        from base.lm.registry import normalize_overlay_llm_model

        config_overlay = dict(config_overlay)
        model_receipt = normalize_overlay_llm_model(
            config_overlay, models=_settings.model_catalog().models
        )
        if model_receipt is not None:
            from loguru import logger

            logger.warning(
                "restart config_overlay llm_model {requested!r} is withdrawn; "
                "storing the registered fallback {resolved!r} — update the "
                "template (task #4306)",
                event="restart_config_normalized",
                requested=model_receipt[0],
                resolved=model_receipt[1],
            )

    agent_id = agent_identity.require_agent_id()
    with process_context.current_context().sql.cursor() as cur, cur.connection.transaction():
        cur.execute("SET TRANSACTION READ WRITE")
        _assert_native_self_action("restart", cur)
        if config_overlay:
            from base.agents.model.restart_config import validate_restart_model_config

            validate_restart_model_config(
                cur,
                agent_id,
                config_overlay,
                catalog=_settings.model_catalog(),
                default_model=_settings.agent_setting("llm_model"),
            )
        _run(cur, plans.restart_statements(agent_id, config_overlay))
    _publish_self_inbound_wake()
    raise AgentRestart


def terminate() -> NoReturn:
    """Stop yourself and end the current execution; your conversation is preserved.

    A new message, including one from background work, can wake you again. Stop
    background work that should no longer wake you before calling this."""
    from meiva.sdk_surface import agent_identity

    ctx = process_context.ambient_context()
    is_attached = (
        ctx is not None
        and ctx.identity is not None
        and ctx.identity.owns_loop
        and ctx.identity.lease is not None
    )
    if not is_attached:
        agent_identity.assert_self_action("terminate")
    target_id = agent_identity.require_agent_id()
    with process_context.current_context().sql.cursor() as cur:
        _run(cur, (plans.terminate_statement(target_id),))
    _publish_self_inbound_wake()
    if is_attached:
        raise SystemExit(0)
    raise AgentTermination


def pause_heartbeat(duration: float) -> None:
    """Suppress idle heartbeat check-ins for the next `duration` seconds.

    Only the heartbeat is suppressed; real wake-ups still reach you. A later
    call replaces the window. For a known wait, prefer the longest window that
    fits it.

    Args:
        duration: seconds, at most the configured heartbeat pause limit (default
            86400 = 24 hours; per-agent override via
            meiva.self.restart(config_overlay=...)).
    """
    duration = coerce_typed(duration, "duration", (int, float))
    from meiva.sdk_surface import agent_identity

    seconds = plans.pause_duration(duration, _settings.agent_setting("heartbeat_pause_max_seconds"))
    agent_id = agent_identity.require_agent_id()
    with process_context.current_context().sql.cursor() as cur:
        _run(cur, plans.heartbeat_pause_statements(agent_id, seconds))
        from base.telemetry import emit

        emit(
            "telemetry",
            "heartbeat_paused",
            level="info",
            agent_id=agent_id,
            # int cast stabilizes the emitted metric kind (task #4011).
            attributes={"duration_s": round(seconds)},
        )


def compact(summary: str) -> NoReturn:
    """Replace the messages in your current context with `summary` and end execution.
    The summary becomes your working record of the earlier conversation. If an
    archived-history note is provided, use its file to recover omitted details.

    First persist durable state: your personal memory (`memory/` in your
    workspace) and the shared pool (`meiva.memory`; facts other agents need). Then
    write `summary`: a first-person record of this conversation round, every
    section filled, "(none)" only when empty.

    Sections: Requests / Progress / In flight / Dead ends / Pitfalls /
    Verbatim tail (exclude the compaction request that triggered this).
    """
    summary = coerce_str(summary, "summary")
    from meiva.sdk_surface import agent_identity

    _assert_native_self_action("compact")
    agent_identity.assert_self_action("compact")
    from base.telemetry import emit_prepared
    from base.telemetry.audit_events import prepare_event_log, record_audit

    # meiva:note
    # Taken before the commit: an SDK root without a random generator fails here, not after the
    # summary is durable and the wake + SystemHalt below have been skipped.
    bus = _settings.bus()
    # The inbound and its audit fact commit together.
    db = process_context.current_context().sql
    with db.transaction(), db.cursor() as cur:
        _assert_native_self_action("compact", cur)
        _run(cur, (plans.compact_statement(agent_identity.require_agent_id(), summary),))
        compact_event = record_audit(
            cur.connection,
            prepare_event_log(
                event_type="compact",
                agent_id=agent_identity.agent_id(),
                source="self",
                payload={"compact_kind": "summary", "length": len(summary)},
            ),
        )
    emit_prepared(compact_event)
    # Best-effort: a publish failure must not stop the wake + SystemHalt below.
    # The durable compact_summary inbound is already committed; if this live-UI
    # event is lost the frontend recovers on its next fetch. Routed through the
    # never-raise primitive so redis can never interrupt this lifecycle exit.
    # Imported here, not at module scope: `import meiva` must not pull the redis /
    # live-events stacks into every exec child (startup-path laziness, task #3816).
    from base.events.live.projection import CompactRequest

    bus.publish_best_effort_sync(
        CompactRequest(
            agent_id=agent_identity.require_agent_id(),
            content=f"[compact requested, {len(summary)} chars]",
        ).model_dump_json(),
        context="compact_request",
    )
    # Also wake the claim node via Redis pub/sub (redundant for same-process
    # but ensures cross-process scenarios also receive the wake).
    _publish_self_inbound_wake()
    raise SystemHalt

"""Agent birth effects inside a caller-owned transaction; no commit, launch or announce."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard
from uuid import UUID

import psycopg

from base import telemetry
from base.agents.birth.creation_identity import record_creation_snapshot, recover_birth
from base.agents.birth.harness import resolve_birth_harness
from base.agents.history.checkpoint_copy import copy_checkpoint_chain
from base.agents.impersonation.manifest import record_central_event
from base.agents.inbox.insert import insert_spawn_prompt_in_transaction
from base.agents.labels import spawn_prompt_with_label
from base.agents.model.birth_config import (
    HARNESS_MODEL_SELECTION_KEY,
    HARNESS_MODEL_SELECTION_VERSION,
    resolve_birth_config,
)
from base.api_contracts.agent_contract import (
    AgentNotFound,
    Harness,
    InvalidModelConfig,
    is_spawner_agent_id_malformed,
    parse_spawner_agent_id,
)
from base.config.service_read import ConfigAuthority
from base.db import fetch_one
from base.db.rows import IdRow, query_one
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit


@dataclass(frozen=True)
class AgentBirth:
    """Transaction result plus audit facts the caller may emit after commit."""

    agent_id: int
    birth_config: dict[str, object] | None
    prompt_inbound_id: int | None
    launch_attempt_id: UUID
    birth_event: telemetry.Event | None
    prompt_event: telemetry.Event | None

    def row_result(self) -> tuple[int, dict[str, object] | None, int | None, UUID]:
        """The public create_agent_row result, including replay's absent prompt."""
        return self.agent_id, self.birth_config, self.prompt_inbound_id, self.launch_attempt_id


def record_birth_event(
    conn: psycopg.Connection,
    agent_id: int,
    spawner: str,
    fork_from: int | None,
    fork_checkpoint: str | None,
    target_machine: str,
) -> telemetry.Event:
    """Record the spawn or fork audit fact in the birth transaction.

    Who spawned whom cannot be derived from any later state, so the row commits
    with the agent row or not at all. A fork's audit parent is the source agent,
    even when a third agent executed it.
    """
    spawner_target: int | None = None
    if spawner.startswith("agent:"):
        spawner_target = int(spawner.removeprefix("agent:"))
    event = prepare_event_log(
        event_type="fork" if fork_from is not None else "spawn",
        agent_id=agent_id,
        source=spawner,
        target_agent_id=fork_from if fork_from is not None else spawner_target,
        payload={
            "machine": target_machine,
            "fork_from": fork_from,
            "fork_checkpoint": fork_checkpoint,
        },
    )
    return record_audit(conn, record_central_event(conn, event))


def validate_spawn_args(
    spawner: str,
    fork_from: int | None,
    fork_checkpoint: str | None,
    prompt: str | None,
    prompt_source: str | None,
) -> None:
    if (fork_from is None) != (fork_checkpoint is None):
        raise ValueError(
            "fork_from and fork_checkpoint must be provided as a pair (both None or both given)"
        )
    if (prompt is None) != (prompt_source is None):
        raise ValueError(
            "prompt and prompt_source must be provided as a pair (both None or both given)"
        )
    if is_spawner_agent_id_malformed(spawner):
        raise ValueError(
            f"spawner has agent: prefix but the id part is not a valid agent id: "
            f"{spawner!r}. This is often caused by an un-bootstrapped process "
            f"(no MeivaContext carrying an agent id was bound) — the process's own agent id "
            f"was None, producing 'agent:None'. Fix the caller to bind "
            f"an identity before spawning."
        )


# meiva:note
# The `RETURNING last_launch_attempt_id` of the agents_meta insert.
@dataclass(frozen=True)
class _LaunchAttemptRow:
    last_launch_attempt_id: UUID


def _insert_agent_identity(conn: psycopg.Connection, label: str | None) -> int:
    # label: when the spawner assigns one, store it sticky (label_user_set=TRUE)
    # so the labeler's CAS (WHERE label IS NULL AND NOT label_user_set) skips it.
    # Otherwise leave NULL — the labeler generates a short name via LLM CAS when
    # spawn carries a prompt; without prompt / on LLM failure the label stays
    # NULL and the frontend displays the fallback "#N".
    if label:
        row = query_one(
            conn,
            IdRow,
            "INSERT INTO agents (label, label_user_set) VALUES (%s, TRUE) RETURNING id",
            (label,),
        )
    else:
        row = query_one(conn, IdRow, "INSERT INTO agents DEFAULT VALUES RETURNING id")
    if row is None:
        raise RuntimeError("expected exactly one row: spawn: insert agent")
    return row.id


def _apply_birth_harness_defaults(
    config: dict[str, object] | None, harness_defaults: Mapping[str, object]
) -> tuple[dict[str, object] | None, dict[str, object]]:
    cleaned = (
        {
            k: v
            for k, v in config.items()
            if not (k in ("llm_model", "reasoning_effort") and v in (None, ""))
        }
        if config
        else config
    )
    overlay_keys = set(cleaned or {})
    inherited = {key: val for key, val in harness_defaults.items() if key not in overlay_keys}
    return cleaned, inherited


def _is_str_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if _is_str_mapping(value) else {}


def _default_reasoning_effort(
    model: str | None,
    catalog: ModelCatalog,
    harness_enum: Harness,
) -> str | None:
    if harness_enum is not Harness.MEIVA or not model:
        return None
    from base.lm.registry import explain_setting

    setting = explain_setting("reasoning_effort", model=model, explicit=None, models=catalog.models)
    return str(setting.value) if setting.value else None


def _resolve_same_harness_inheritance(
    parent_model: object | None,
    parent_effort: object | None,
    req_model: object | None,
    req_effort: object | None,
    catalog: ModelCatalog,
    harness_enum: Harness,
) -> dict[str, object] | None:
    inherited: dict[str, object] = {}
    effective_model = req_model or parent_model
    if not req_model:
        inherited["llm_model"] = parent_model
    if req_effort in (None, ""):
        if (req_model is None or req_model == parent_model) and parent_effort is not None:
            inherited["reasoning_effort"] = parent_effort
        else:
            effort = _default_reasoning_effort(
                str(effective_model) if effective_model else None, catalog, harness_enum
            )
            if effort:
                inherited["reasoning_effort"] = effort
    return inherited or None


def _resolve_parent_spawn_inheritance(
    cur: psycopg.Cursor[tuple[object, ...]],
    parent_id: int,
    config: dict[str, object] | None,
    harness: str,
    catalog: ModelCatalog,
) -> dict[str, object] | None:
    harness_enum = Harness(harness)
    cur.execute(
        "SELECT harness, config_overlay, birth_config FROM agents_meta WHERE id = %s",
        (parent_id,),
    )
    p_row = cur.fetchone()
    if p_row is None:
        raise AgentNotFound(f"parent agent {parent_id} does not exist")
    parent_harness = str(p_row[0])
    parent_overlay = _as_mapping(p_row[1])
    parent_birth = _as_mapping(p_row[2])
    parent_model = (
        parent_overlay.get("llm_model")
        if "llm_model" in parent_overlay
        else parent_birth.get("llm_model")
    )
    parent_effort = (
        parent_overlay.get("reasoning_effort")
        if "reasoning_effort" in parent_overlay
        else parent_birth.get("reasoning_effort")
    )
    req_model = config.get("llm_model") if config else None
    req_effort = config.get("reasoning_effort") if config else None

    if harness != parent_harness:
        if not req_model:
            raise ValueError(
                f"llm_model must be explicitly provided when spawning on a different harness ({harness} != {parent_harness})"
            )
        if req_effort in (None, ""):
            effort = _default_reasoning_effort(str(req_model), catalog, harness_enum)
            if effort:
                return {"reasoning_effort": effort}
        return None

    return _resolve_same_harness_inheritance(
        parent_model, parent_effort, req_model, req_effort, catalog, harness_enum
    )


def _resolve_fork_overlay_and_inherited(
    cur: psycopg.Cursor[tuple[object, ...]],
    fork_from: int,
    config: dict[str, object] | None,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    cur.execute("SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (fork_from,))
    stored = fetch_one(cur, "spawn: read fork source config")
    s_overlay = _as_mapping(stored[0])
    s_birth = _as_mapping(stored[1])
    if stored[1] is not None and not isinstance(stored[1], dict):
        raise TypeError(f"agents_meta.birth_config of agent {fork_from} is not a JSON object")
    resolved_config = dict(s_overlay) if (config is None and s_overlay) else config
    inherited = dict(s_birth) if stored[1] is not None else None
    return resolved_config, inherited


def _resolve_root_spawn_inherited(
    config: dict[str, object] | None,
    harness_enum: Harness,
    catalog: ModelCatalog,
) -> dict[str, object] | None:
    req_model = config.get("llm_model") if config else None
    req_effort = config.get("reasoning_effort") if config else None
    if not req_model:
        raise ValueError("llm_model must be explicitly provided for root/non-agent spawn")
    if req_effort in (None, ""):
        effort = _default_reasoning_effort(str(req_model), catalog, harness_enum)
        if effort:
            return {"reasoning_effort": effort}
    return None


def _resolve_birth_overlay(
    cur: psycopg.Cursor[tuple[object, ...]],
    config: dict[str, object] | None,
    fork_from: int | None,
    *,
    spawner: str = "user",
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    harness: str,
    harness_defaults: Mapping[str, object] | None = None,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    from base.config.agent_pins import resolve_agent_config_pins
    from base.lm.factory import validate_model_config

    harness_enum = Harness(harness)
    if fork_from is not None:
        config, inherited = _resolve_fork_overlay_and_inherited(cur, fork_from, config)
    elif (parent_id := parse_spawner_agent_id(spawner)) is not None:
        inherited = _resolve_parent_spawn_inheritance(cur, parent_id, config, harness, catalog)
    else:
        inherited = _resolve_root_spawn_inherited(config, harness_enum, catalog)

    if harness_defaults:
        config, h_inherited = _apply_birth_harness_defaults(config, harness_defaults)
        inherited = {**(inherited or {}), **h_inherited}
    if config and harness_enum is Harness.MEIVA:
        config = dict(config)
        model_receipt = normalize_overlay_llm_model(config, models=catalog.models)
        if model_receipt is not None:
            logger.warning(
                "spawn overlay llm_model {requested!r} is withdrawn; stored "
                "the registered fallback {resolved!r} (task #4306)",
                event="spawn_overlay_model_normalized",
                requested=model_receipt[0],
                resolved=model_receipt[1],
            )
    birth_config = resolve_birth_config(config, inherited=inherited, authority=authority)

    try:
        pins = resolve_agent_config_pins(config, birth_config)
        validate_model_config(catalog=catalog, config=pins, harness=harness_enum)
    except ValueError as exc:
        raise InvalidModelConfig(str(exc)) from exc
    if harness_enum is not Harness.MEIVA:
        birth_config[HARNESS_MODEL_SELECTION_KEY] = HARNESS_MODEL_SELECTION_VERSION
    return config, birth_config


def _insert_fork_history(
    cur: psycopg.Cursor[tuple[object, ...]],
    new_id: int,
    fork_from: int | None,
    fork_checkpoint: str | None,
    fork_tail_skills: list[str] | None,
) -> None:
    if fork_from is not None and fork_checkpoint is not None:
        # ForkCheckpointNotFound causes the whole transaction to roll back (the with block does not commit)
        copy_checkpoint_chain(cur, fork_from, fork_checkpoint, new_id)
        # The copied history reads as the source agent's identity to the new
        # process. INSERT a kind='fork' lifecycle inbound in THIS transaction
        # (committed before launch) so the new agent's first claim appends an
        # identity marker before any LLM turn. source carries the lineage
        # "agent:{fork_from}" (intrinsic — independent of `spawner`); the
        # claim node renders the new id from its own config.
        # The fork inbound carries the tail-graft skill delta (skills the
        # fork's config added to skills_to_inject_into_system_prompt beyond
        # the source's, minus what the fork's expand list already grafts).
        # The claim node's fork handler appends their SKILL.md bodies at the
        # context tail — the inherited prefix stays byte-identical for the
        # provider cache. payload NULL = nothing to graft (legacy forks too).
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, '', 'fork', %s, %s::jsonb)",
            (
                new_id,
                f"agent:{fork_from}",
                json.dumps({"tail_skills": fork_tail_skills}) if fork_tail_skills else None,
            ),
        )


def insert_agent_birth(
    cur: psycopg.Cursor[tuple[object, ...]],
    *,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    spawner: str = "user",
    fork_from: int | None = None,
    fork_checkpoint: str | None = None,
    machine: str,
    config: dict[str, object] | None = None,
    label: str | None = None,
    prompt: str | None = None,
    prompt_source: str | None = None,
    fork_tail_skills: list[str] | None = None,
    harness: str | None = None,
    harness_defaults: Mapping[str, object] | None = None,
    creation_key: str | None = None,
    creation_request_hash: str | None = None,
) -> AgentBirth:
    """Write/replay the existing birth, first prompt, fork and audit in this transaction.

    A keyed birth (`creation_key` with `creation_request_hash`) also retains its
    immutable `agent_creation_snapshots` record in the same transaction. Caller
    owns commit/rollback and post-commit announcements. Replay returns no new
    birth event, prompt event or prompt inbound id.
    """
    validate_spawn_args(spawner, fork_from, fork_checkpoint, prompt, prompt_source)
    # The gateway creates the row for ANY target (the runner's ops server runs
    # as meiva_runner and cannot INSERT agents); the launch op re-checks the
    # agent-runner capability on the target itself.
    target_machine = machine
    conn = cur.connection

    prompt_inbound_id: int | None = None
    prompt_event: telemetry.Event | None = None
    existing = recover_birth(conn, creation_key, creation_request_hash)
    if existing is not None:
        return AgentBirth(
            existing.agent_id, existing.birth_config, None, existing.launch_attempt_id, None, None
        )
    # meiva:note
    # Settled before anything is written: a fork naming a different harness than its
    # source is refused here, so a caller that skipped request validation cannot
    # bypass the rule.
    # meiva:link base/agents/docs/harness/harness.meiva.okf.md
    born_harness = resolve_birth_harness(cur, harness, fork_from, spawner=spawner)
    new_id = _insert_agent_identity(conn, label)
    config, birth_config = _resolve_birth_overlay(
        cur,
        config,
        fork_from,
        spawner=spawner,
        catalog=catalog,
        authority=authority,
        harness=born_harness,
        harness_defaults=harness_defaults,
    )
    # For a fork, spawner records the fork SOURCE — the lineage parent
    # (user ruling 2026-08-28, task #1879). The executor who triggered the
    # fork stays traceable via the fork event's `source` and the fork
    # prompt inbound's source; it is not what the spawner column means.
    lineage_spawner = f"agent:{fork_from}" if fork_from is not None else spawner
    launch_row = query_one(
        conn,
        _LaunchAttemptRow,
        "INSERT INTO agents_meta (id, spawner, born_spawner, fork_source_agent_id, "
        "fork_source_checkpoint_id, status, machine, harness, config_overlay, birth_config, "
        "last_launch_attempt_id) "
        "VALUES (%s, %s, %s, %s, %s, 'idling', %s, %s, %s::jsonb, %s::jsonb, gen_random_uuid()) "
        "RETURNING last_launch_attempt_id",
        (
            new_id,
            lineage_spawner,
            lineage_spawner,
            fork_from,
            fork_checkpoint,
            target_machine,
            born_harness,
            json.dumps(config) if config else None,
            json.dumps(birth_config, sort_keys=True),
        ),
    )
    if launch_row is None:
        raise RuntimeError("expected exactly one row: spawn: insert agent meta")
    launch_attempt_id = launch_row.last_launch_attempt_id
    _insert_fork_history(cur, new_id, fork_from, fork_checkpoint, fork_tail_skills)
    if prompt is not None:
        assert prompt_source is not None, "prompt requires prompt_source (validated above)"  # noqa: S101
        prompt_content = spawn_prompt_with_label(prompt, label)
        prompt_inbound_id, prompt_event = insert_spawn_prompt_in_transaction(
            cur, new_id, prompt_content, prompt_source
        )
    if creation_key is not None and creation_request_hash is not None:
        record_creation_snapshot(
            conn,
            key=creation_key,
            request_hash=creation_request_hash,
            agent_id=new_id,
            machine=target_machine,
            config=config,
            birth_config=birth_config,
            launch_attempt_id=launch_attempt_id,
            prompt_inbound_id=prompt_inbound_id,
            prompt_content=spawn_prompt_with_label(prompt, label) if prompt is not None else None,
            prompt_source=prompt_source,
        )
    birth_event = record_birth_event(
        conn, new_id, spawner, fork_from, fork_checkpoint, target_machine
    )
    return AgentBirth(
        new_id, birth_config, prompt_inbound_id, launch_attempt_id, birth_event, prompt_event
    )

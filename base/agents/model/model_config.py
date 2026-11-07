"""Validate the effective model configuration before agent configuration writes."""

from collections.abc import Mapping, Sequence
from typing import TypeGuard

import psycopg

from base.agents.model.birth_config import resolve_birth_config
from base.agents.model.harness_models import resolve_probed_harness_selection
from base.agents.model.restart_config import (
    RestartHarnessTarget as RestartHarnessTarget,
)
from base.agents.model.restart_config import (
    validate_restart_model_config as validate_restart_model_config,
)
from base.api_contracts.agent_contract import (
    AgentNotFound,
    Harness,
    InvalidModelConfig,
    ProbedHarnessModel,
    parse_spawner_agent_id,
)
from base.config.agent_pins import resolve_agent_config_pins
from base.config.service_read import ConfigAuthority
from base.lm import factory
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys


def _resolve_probed_spawn_defaults(
    harness: Harness,
    config: dict[str, object] | None,
    probed_models: Sequence[ProbedHarnessModel],
) -> tuple[dict[str, object] | None, dict[str, object]]:
    resolved_model, resolved_effort = resolve_probed_harness_selection(
        harness,
        probed_models,
        requested_model=config.get("llm_model") if config else None,
        requested_effort=config.get("reasoning_effort") if config else None,
    )
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
    inherited: dict[str, object] = {
        key: val
        for key, val in (
            ("llm_model", resolved_model),
            ("reasoning_effort", resolved_effort or ""),
        )
        if key not in overlay_keys
    }
    return cleaned, inherited


def _is_str_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if _is_str_mapping(value) else {}


def _resolve_spawn_fork_source(
    cur: psycopg.Cursor[tuple[object, ...]],
    fork_from: int,
    config: dict[str, object] | None,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    cur.execute("SELECT config_overlay, birth_config FROM agents_meta WHERE id=%s", (fork_from,))
    row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"fork source agent {fork_from} does not exist")
    s_overlay = _as_mapping(row[0])
    s_birth = _as_mapping(row[1])
    if row[1] is not None and not isinstance(row[1], dict):
        raise TypeError(f"agents_meta.birth_config of agent {fork_from} is not a JSON object")
    resolved_config = dict(s_overlay) if (config is None and s_overlay) else config
    inherited = dict(s_birth) if row[1] is not None else None
    return resolved_config, inherited


def _resolve_spawn_parent_inheritance(
    cur: psycopg.Cursor[tuple[object, ...]],
    parent_id: int,
    config: dict[str, object] | None,
    harness: Harness,
) -> dict[str, object] | None:
    cur.execute(
        "SELECT harness, config_overlay, birth_config FROM agents_meta WHERE id=%s",
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

    if harness.value != parent_harness:
        if not req_model:
            raise InvalidModelConfig(
                f"llm_model must be explicitly provided when spawning on a different harness ({harness.value} != {parent_harness})"
            )
        return None

    inherited: dict[str, object] = {}
    if not req_model:
        inherited["llm_model"] = parent_model
    if (
        req_effort in (None, "")
        and parent_effort is not None
        and (req_model is None or req_model == parent_model)
    ):
        inherited["reasoning_effort"] = parent_effort
    return inherited or None


# meiva:note
# Check the model, effort and provider key a plain birth or inherited fork will run.
def validate_spawn_model_config(
    cur: psycopg.Cursor[tuple[object, ...]],
    config: dict[str, object] | None,
    fork_from: int | None = None,
    *,
    spawner: str | None = None,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    llm_override: str,
    authority: ConfigAuthority,
    harness: Harness = Harness.MEIVA,
    probed_models: Sequence[ProbedHarnessModel] | None = None,
) -> str:
    inherited: dict[str, object] | None = None
    if fork_from is not None:
        config, inherited = _resolve_spawn_fork_source(cur, fork_from, config)
    elif spawner and (parent_id := parse_spawner_agent_id(spawner)) is not None:
        inherited = _resolve_spawn_parent_inheritance(cur, parent_id, config, harness)

    if harness is not Harness.MEIVA and probed_models is not None:
        effective_cfg = {**(inherited or {}), **(config or {})}
        cleaned, h_inherited = _resolve_probed_spawn_defaults(harness, effective_cfg, probed_models)
        inherited = {**(inherited or {}), **h_inherited}
        if config is not None and cleaned is not None:
            config = {k: v for k, v in cleaned.items() if k in config}
    try:
        birth = resolve_birth_config(config, inherited=inherited, authority=authority)
        return factory.validate_model_launch(
            config=resolve_agent_config_pins(config, birth),
            catalog=catalog,
            keys=keys,
            llm_override=llm_override,
            harness=harness,
        )
    except ValueError as exc:
        raise InvalidModelConfig(str(exc)) from exc

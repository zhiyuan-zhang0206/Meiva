"""Validate the effective model configuration before agent configuration writes."""

from collections.abc import Sequence

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
)
from base.config.agent_pins import resolve_agent_config_pins
from base.config.service_read import ConfigAuthority
from base.host.boundary.json_values import is_object_dict
from base.lm import factory
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys


def _resolve_probed_spawn_defaults(
    harness: Harness,
    config: dict[str, object] | None,
    probed_models: Sequence[ProbedHarnessModel],
    probed_default_model: str | None,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    resolved_model, resolved_effort = resolve_probed_harness_selection(
        harness,
        probed_models,
        probed_default_model,
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


# meiva:note
# Check the model, effort and provider key a plain birth or inherited fork will run.
def validate_spawn_model_config(
    cur: psycopg.Cursor[tuple[object, ...]],
    config: dict[str, object] | None,
    fork_from: int | None = None,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    llm_override: str,
    authority: ConfigAuthority,
    harness: Harness = Harness.MEIVA,
    probed_models: Sequence[ProbedHarnessModel] | None = None,
    probed_default_model: str | None = None,
) -> str:
    inherited: dict[str, object] | None = None
    if fork_from is not None:
        cur.execute("SELECT birth_config FROM agents_meta WHERE id=%s", (fork_from,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"fork source agent {fork_from} does not exist")
        stored = row[0]
        if stored is not None and not is_object_dict(stored):
            raise TypeError(f"agents_meta.birth_config of agent {fork_from} is not a JSON object")
        inherited = stored
    elif harness is not Harness.MEIVA and probed_models is not None:
        config, inherited = _resolve_probed_spawn_defaults(
            harness, config, probed_models, probed_default_model
        )
    try:
        birth = resolve_birth_config(
            cur, config, inherited=inherited, catalog=catalog, authority=authority
        )
        return factory.validate_model_launch(
            config=resolve_agent_config_pins(config, birth),
            catalog=catalog,
            keys=keys,
            llm_override=llm_override,
            harness=harness,
        )
    except ValueError as exc:
        raise InvalidModelConfig(str(exc)) from exc

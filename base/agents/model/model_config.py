"""Validate the effective model configuration before agent configuration writes."""

import psycopg

from base.agents.model.birth_config import resolve_birth_config
from base.agents.model.restart_config import (
    validate_restart_model_config as validate_restart_model_config,
)
from base.api_contracts.agent_contract import AgentNotFound, Harness, InvalidModelConfig
from base.config.agent_pins import resolve_agent_config_pins
from base.config.service_read import ConfigAuthority
from base.lm import factory
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys


# meiva:note
# Check the model, effort and provider key a plain birth or inherited fork will run.
def validate_spawn_model_config(
    cur: psycopg.Cursor,
    config: dict[str, object] | None,
    fork_from: int | None = None,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    llm_override: str,
    authority: ConfigAuthority,
    harness: Harness = Harness.MEIVA,
) -> str:
    inherited = None
    if fork_from is not None:
        cur.execute("SELECT birth_config FROM agents_meta WHERE id=%s", (fork_from,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"fork source agent {fork_from} does not exist")
        inherited = row[0]
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

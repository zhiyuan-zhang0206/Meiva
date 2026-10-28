from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.agents.model.birth_config import set_cluster_default_model
from base.agents.model.model_config import (
    validate_restart_model_config,
    validate_spawn_model_config,
)
from base.api_contracts.agent_contract import Harness, InvalidModelConfig
from base.config import Settings, settings
from base.config.service_read import ConfigAuthority
from base.db import create_agent
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.tests.model_catalog import inert_keys


def test_external_spawn_validates_execution_selection_without_native_keys(
    db_conn: psycopg.Connection, tmp_path: Path, *, model_catalog: ModelCatalog
) -> None:
    authority = ConfigAuthority(settings, Settings(profile=None), tmp_path / ".env")
    with db_conn.cursor() as cur:
        assert (
            validate_spawn_model_config(
                cur,
                {"llm_model": "gpt-6-astra", "reasoning_effort": "ultra"},
                catalog=model_catalog,
                keys=ProviderKeys({}),
                llm_override="",
                authority=authority,
                harness=Harness.CODEX,
            )
            == "gpt-6-astra"
        )
        with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
            validate_spawn_model_config(
                cur,
                {"llm_model": "gpt-6-astra", "reasoning_effort": "ultra"},
                catalog=model_catalog,
                keys=ProviderKeys({}),
                llm_override="",
                authority=authority,
                harness=Harness.MEIVA,
            )
        with pytest.raises(InvalidModelConfig, match="not supported by harness"):
            validate_spawn_model_config(
                cur,
                {"llm_model": "claude-opus-5-5", "reasoning_effort": "high"},
                catalog=model_catalog,
                keys=ProviderKeys({}),
                llm_override="",
                authority=authority,
                harness=Harness.CODEX,
            )


def test_fork_preflight_keeps_its_birth_model_after_default_changes(
    db_conn: psycopg.Connection,
    cluster_defaults_unset: None,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,birth_config) VALUES(%s,'idling','model-config-test',%s)",
        (agent_id, Jsonb({"llm_model": "deepseek-flash", "reasoning_effort": "max"})),
    )
    with db_conn.cursor() as cur:
        set_cluster_default_model(cur, "mimo-v2.6-pro", updated_by="test")
        assert (
            validate_spawn_model_config(
                cur,
                None,
                agent_id,
                catalog=model_catalog,
                keys=inert_keys(model_catalog),
                llm_override=settings.lm.llm_override,
                authority=ConfigAuthority(settings, Settings(profile=None), tmp_path / ".env"),
            )
            == "deepseek-flash"
        )


def test_restart_validates_birth_effort_when_only_the_model_changes(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,birth_config) VALUES(%s,'idling','model-config-test',%s)",
        (agent_id, Jsonb({"llm_model": "gpt-5.6-sol", "reasoning_effort": "low"})),
    )
    with db_conn.cursor() as cur:
        with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
            validate_restart_model_config(
                cur,
                agent_id,
                {"llm_model": "deepseek-flash"},
                catalog=model_catalog,
                default_model=settings.lm.llm_model,
            )
        validate_restart_model_config(
            cur,
            agent_id,
            {"llm_model": "deepseek-flash", "reasoning_effort": "max"},
            catalog=model_catalog,
            default_model=settings.lm.llm_model,
        )


def test_legacy_restart_validates_effort_against_the_unpinned_model(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling','model-config-test')",
        (agent_id,),
    )
    with db_conn.cursor() as cur:
        with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
            validate_restart_model_config(
                cur,
                agent_id,
                {"reasoning_effort": "max"},
                catalog=model_catalog,
                default_model="mimo-v2.6-pro",
            )
        validate_restart_model_config(
            cur,
            agent_id,
            {"reasoning_effort": "high"},
            catalog=model_catalog,
            default_model="mimo-v2.6-pro",
        )

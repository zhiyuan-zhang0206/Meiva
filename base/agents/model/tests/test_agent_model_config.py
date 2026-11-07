from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.agents.model.model_config import (
    RestartHarnessTarget,
    validate_restart_model_config,
    validate_spawn_model_config,
)
from base.api_contracts.agent_contract import (
    Harness,
    InvalidModelConfig,
    ProbedHarnessModel,
)
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
    codex_models = (
        ProbedHarnessModel(
            id="gpt-6.1-terra",
            reasoning_effort_options=("low", "medium", "high", "ultra"),
            reasoning_effort_default="low",
        ),
    )
    agy_models = (
        ProbedHarnessModel(
            id="gpt-oss-120b",
            reasoning_effort_options=("medium",),
            reasoning_effort_default="medium",
        ),
    )
    with db_conn.cursor() as cur:
        assert (
            validate_spawn_model_config(
                cur,
                {"llm_model": "gpt-6.1-terra", "reasoning_effort": "ultra"},
                catalog=model_catalog,
                keys=ProviderKeys({}),
                llm_override="",
                authority=authority,
                harness=Harness.CODEX,
                probed_models=codex_models,
            )
            == "gpt-6.1-terra"
        )
        assert (
            validate_spawn_model_config(
                cur,
                {"llm_model": "gpt-oss-120b"},
                catalog=model_catalog,
                keys=ProviderKeys({}),
                llm_override="",
                authority=authority,
                harness=Harness.ANTIGRAVITY,
                probed_models=agy_models,
            )
            == "gpt-oss-120b"
        )
        with pytest.raises(InvalidModelConfig, match="no model configured"):
            validate_spawn_model_config(
                cur,
                None,
                catalog=model_catalog,
                keys=ProviderKeys({}),
                llm_override="",
                authority=authority,
                harness=Harness.ANTIGRAVITY,
                probed_models=agy_models,
            )
        with pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"):
            validate_spawn_model_config(
                cur,
                {"llm_model": "gpt-6.1-terra", "reasoning_effort": "ultra"},
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
                probed_models=codex_models,
            )


def test_restart_switching_onto_external_harness_drops_prior_harness_pins(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,harness,birth_config) "
        "VALUES(%s,'idling','model-config-test','meiva',%s)",
        (agent_id, Jsonb({"llm_model": "deepseek-flash", "reasoning_effort": "max"})),
    )
    probed = (
        ProbedHarnessModel(
            id="gpt-6.1-terra",
            reasoning_effort_options=("low", "high", "ultra"),
            reasoning_effort_default="low",
        ),
    )
    with db_conn.cursor() as cur:
        with pytest.raises(
            InvalidModelConfig, match="llm_model must be explicitly provided in config_overlay"
        ):
            validate_restart_model_config(
                cur,
                agent_id,
                {"harness": "codex"},
                catalog=model_catalog,
                default_model=settings.lm.llm_model,
                target=RestartHarnessTarget(
                    harness=Harness.CODEX,
                    probed_models=probed,
                ),
            )
        resolved = validate_restart_model_config(
            cur,
            agent_id,
            {"harness": "codex", "llm_model": "gpt-6.1-terra"},
            catalog=model_catalog,
            default_model=settings.lm.llm_model,
            target=RestartHarnessTarget(
                harness=Harness.CODEX,
                probed_models=probed,
            ),
        )
        assert resolved == ("gpt-6.1-terra", "low")


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

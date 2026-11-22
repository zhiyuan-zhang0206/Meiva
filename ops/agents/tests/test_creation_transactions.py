import psycopg
import pytest

from base.agents.birth.creation_identity import creation_request_hash
from base.agents.birth.transaction import insert_agent_birth
from base.api_contracts.agent_contract import InvalidModelConfig
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


def _counts(conn: psycopg.Connection) -> tuple[int, ...]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM agents), (SELECT count(*) FROM agents_meta), "
        "(SELECT count(*) FROM audit_events), "
        "(SELECT count(*) FROM inbound_messages)"
    ).fetchone()
    assert row is not None
    return row


def test_actual_birth_model_effort_failure_rolls_back_every_effect(
    db_conn: psycopg.Connection,
    cluster_defaults_unset: None,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    before = _counts(db_conn)
    with (
        pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"),
        db_conn.transaction(),
        db_conn.cursor() as cur,
    ):
        insert_agent_birth(
            cur,
            machine=machine_identity.name(),
            config={"llm_model": "mimo-v2.6-pro", "reasoning_effort": "max"},
            prompt="This birth must roll back",
            prompt_source="user",
            catalog=build_model_catalog(home=meiva_home()),
            authority=config_authority,
        )
    assert _counts(db_conn) == before


def test_cursor_birth_replay_returns_original_attempt_without_new_effects(
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    with db_conn.transaction(), db_conn.cursor() as cur:
        original = insert_agent_birth(
            cur,
            machine=machine_identity.name(),
            config={"llm_model": "deepseek-flash"},
            prompt="First instruction",
            prompt_source="user",
            creation_key="transaction-birth",
            creation_request_hash=creation_request_hash({"prompt": "First instruction"}),
            catalog=build_model_catalog(home=meiva_home()),
            authority=config_authority,
        )
    before = _counts(db_conn)
    db_conn.commit()
    with db_conn.transaction(), db_conn.cursor() as cur:
        replay = insert_agent_birth(
            cur,
            machine=machine_identity.name(),
            config={"llm_model": "deepseek-flash"},
            prompt="First instruction",
            prompt_source="user",
            creation_key="transaction-birth",
            creation_request_hash=creation_request_hash({"prompt": "First instruction"}),
            catalog=build_model_catalog(home=meiva_home()),
            authority=config_authority,
        )
        assert replay.agent_id == original.agent_id
        assert replay.launch_attempt_id == original.launch_attempt_id
        assert replay.birth_config == original.birth_config
        assert replay.birth_event is None
        assert replay.prompt_event is None and replay.prompt_inbound_id is None
        assert _counts(db_conn) == before

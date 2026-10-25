# meiva:note
# `accept_retry_launch`: one operation key commits one retry intent with one immutable attempt.

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.birth.spawn import create_agent_row
from base.api_contracts.launch_retry import RetryLaunchRequest
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.packages.plugin_providers import build_model_catalog
from ops.agents.launch_retry import RetryLaunchConflictError, accept_retry_launch


def _prior_attempt(
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> tuple[int, UUID]:
    agent_id, _birth, _prompt, attempt = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    return agent_id, attempt


# meiva:note
# The attempt id the caller supplies becomes the agent's pointer when the intent commits, and a
# replay of the same key returns the stored acceptance and leaves the pointer alone.
def test_the_supplied_attempt_commits_once_and_a_replay_returns_the_first(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id, prior = _prior_attempt(database, event_bus, config_authority, machine_identity)
    body = RetryLaunchRequest(expected_prior_attempt_id=prior)
    first_attempt, replay_attempt = uuid4(), uuid4()

    machine, accepted = accept_retry_launch(
        db_pool, "retry-key", agent_id, body, new_attempt_id=first_attempt
    )
    replayed_machine, replayed = accept_retry_launch(
        db_pool, "retry-key", agent_id, body, new_attempt_id=replay_attempt
    )

    assert machine == replayed_machine == machine_identity.name()
    assert accepted.launch_attempt_id == first_attempt
    assert replayed == accepted
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (first_attempt,)


# meiva:note
# A different observed attempt under a new key is a conflict, and the pointer stays where it was.
def test_a_stale_observed_attempt_conflicts_without_moving_the_pointer(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id, prior = _prior_attempt(database, event_bus, config_authority, machine_identity)
    body = RetryLaunchRequest(expected_prior_attempt_id=uuid4())

    with pytest.raises(RetryLaunchConflictError, match="stale"):
        accept_retry_launch(db_pool, "stale-key", agent_id, body, new_attempt_id=uuid4())

    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (prior,)

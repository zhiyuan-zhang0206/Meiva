# meiva:note
# The lifecycle entries of `meiva.self` against the real throwaway Postgres and Redis: the
# statements `meiva/self/plans.py` returns are executed on the agent's own database, and the
# wake they send is received on the agent's inbound channel. The statements themselves are
# asserted as values in `test_self_plans.py`; this file only checks that the shell runs them.
# meiva:link meiva/self/docs/self.meiva.okf.md

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

import meiva
from base.agents.context.identity import ExternalLease
from base.agents.lifecycle import AgentRestart, AgentTermination, SystemHalt
from base.agents.tests.units import spawn_agent
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import CompactRequest
from base.events.live.redis_client import open_sync_redis
from base.events.live.tests.rng import seeded_auth_retry
from base.lm.catalog import ModelCatalog
from meiva.sdk_surface import process_context
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva.tests.fixtures.sdk import pinned_bus as pinned_bus
from meiva.tests.namespaces.self.redis_view import loose


def _inbound(db_conn: psycopg.Connection[Any], agent_id: int, kind: str) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT source, content, payload FROM inbound_messages "
        "WHERE agent_id = %s AND kind = %s ORDER BY id",
        (agent_id, kind),
    ).fetchall()


def _spawned(
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    *,
    harness: str | None = None,
) -> int:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
        harness=harness,
        config={"llm_model": "gpt-6.1-sol", "reasoning_effort": "high"}
        if harness == "codex"
        else None,
    )
    pin_agent(agent_id)
    return agent_id


def test_terminate_queues_its_inbound_row_and_wakes_the_agents_own_channel(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)
    listener = open_sync_redis(redis_url, decode_responses=True, auth_retry=seeded_auth_retry())
    subscription = loose(listener).pubsub()
    subscription.subscribe(inbound_channel(agent_id, settings.data_plane.events_channel))
    assert subscription.get_message(timeout=5)["type"] == "subscribe"

    with pytest.raises(AgentTermination):
        meiva.self.terminate()

    assert _inbound(db_conn, agent_id, "terminate") == [("self", "", None)]
    woken = subscription.get_message(timeout=5)
    subscription.close()
    listener.close()
    assert woken is not None and woken["data"] == "0"


def test_restart_without_an_overlay_queues_a_bare_restart_row(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)

    with pytest.raises(AgentRestart):
        meiva.self.restart()

    assert _inbound(db_conn, agent_id, "restart") == [("self", "", None)]
    overlay = db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert overlay is not None and not overlay[0]


@pytest.mark.usefixtures("sdk_model_owner")
def test_pause_heartbeat_logs_the_pause_and_sets_the_agents_paused_until(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)

    meiva.self.pause_heartbeat(90)

    logged = db_conn.execute(
        "SELECT duration_s FROM heartbeat_pause_log WHERE agent_id = %s", (agent_id,)
    ).fetchall()
    assert logged == [(90.0,)]
    paused = db_conn.execute(
        "SELECT heartbeat_paused_until > now() + interval '60 seconds' FROM agents_meta "
        "WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert paused == (True,)


@pytest.mark.usefixtures("sdk_model_owner")
def test_pause_heartbeat_above_the_agents_limit_writes_nothing(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)

    with pytest.raises(ValueError, match="at most"):
        meiva.self.pause_heartbeat(10**9)

    count = db_conn.execute(
        "SELECT count(*) FROM heartbeat_pause_log WHERE agent_id = %s", (agent_id,)
    ).fetchone()
    assert count == (0,)


# meiva:note
# A stored harness other than the native one refuses the lifecycle entries that belong to the
# native loop: the row the database holds for the agent is the third place `_assert_native_self_action`
# reads, and this pins that the shell's in-transaction query is what refuses.
def test_restart_is_refused_for_the_harness_stored_for_the_agent(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(
        model_catalog, config_authority, database_gate, machine_identity, harness="codex"
    )

    with pytest.raises(RuntimeError, match="codex"):
        meiva.self.restart()

    assert _inbound(db_conn, agent_id, "restart") == []


# meiva:note
# A controller attached through an external lease that owns the loop ends its own process instead
# of raising the agent's termination; the decision is `lifecycle_plans.is_attached_controller`, and
# this pins that the shell acts on it (and skips the self-action check only then).
def test_terminate_of_an_attached_controller_exits_without_borrowing_the_agents_identity(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)
    pin_agent(
        agent_id,
        owns_loop=True,
        lease=ExternalLease(agent_id=agent_id, validate=lambda: agent_id, config=lambda: None),
    )

    with pytest.raises(SystemExit) as exit_info:
        meiva.self.terminate()

    assert exit_info.value.code == 0
    assert _inbound(db_conn, agent_id, "terminate") == [("self", "", None)]


# meiva:note
# Install a plugin whose config declares `field` as a per-agent setting under `home`: the home a
# test hands in is the only one that knows the field.
def _write_probe_plugin(home: Path, field: str) -> None:
    plugin_dir = home / "plugins" / "restart_probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text('__description__ = "restart probe"\n', encoding="utf-8")
    (plugin_dir / "default_config.py").write_text(
        f"""from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class ProbeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    {field}: str = Field(default="unset", json_schema_extra={{"per_agent": True}})


def contribute() -> PluginContributions:
    return PluginContributions(config=ProbeConfig)
""",
        encoding="utf-8",
    )


# meiva:note
# The overlay a restart validates is checked against the plugin configs installed in the Meiva home
# the bound context carries, not the process's own: the probe plugin exists only under the context's
# home, so a home read from the environment would refuse the overlay as an unknown key.
@pytest.mark.usefixtures("sdk_model_owner")
def test_restart_checks_the_overlay_against_the_plugin_configs_of_the_contexts_home(
    tmp_path: Path,
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = _spawned(model_catalog, config_authority, database_gate, machine_identity)
    overlay_home = tmp_path / "overlay-home"
    _write_probe_plugin(overlay_home, "restart_probe")
    bound = process_context.current_context()
    process_context.unbind_context()
    process_context.bind_context(replace(bound, meiva_home=overlay_home))
    assert overlay_home != Path.home()

    with pytest.raises(AgentRestart):
        meiva.self.restart({"restart_probe": "x"})

    stored = db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert stored is not None and stored[0] == {"restart_probe": "x"}


# meiva:note
# The live request a compaction publishes carries the text `compact_notice` decided: the summary's
# length only, never its text. The wake of the same compaction travels on the agent's inbound
# channel; this reads the bus channel the frontend listens on.
def test_compact_publishes_the_notice_text_of_its_summary(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    pinned_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    _spawned(model_catalog, config_authority, database_gate, machine_identity)
    listener = open_sync_redis(redis_url, decode_responses=True, auth_retry=seeded_auth_retry())
    subscription = loose(listener).pubsub()
    subscription.subscribe(pinned_bus.channel)
    assert subscription.get_message(timeout=5)["type"] == "subscribe"
    summary = "Requests: (none)\nProgress: done\n"

    with pytest.raises(SystemHalt):
        meiva.self.compact(summary)

    message = subscription.get_message(timeout=5)
    subscription.close()
    listener.close()
    assert message is not None
    request = CompactRequest.model_validate_json(message["data"])
    assert request.role == "compact_request"
    assert request.content == f"[compact requested, {len(summary)} chars]"
    assert "Progress" not in request.content

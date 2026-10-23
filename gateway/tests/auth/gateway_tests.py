# meiva:note
# Gateway test fixtures (registered by `tests/fixtures/path_scopes.py`).

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool

from base.cluster import machines as _machines
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.config.tests.api_keys import mock_api_keys as mock_api_keys
from base.db import Database
from base.events.live.bus import EventBus
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import model_catalog as model_catalog
from gateway.agents import creation as _agents_creation
from gateway.agents import forward as _agents_forward_router
from ops.lifecycle import launch_agent_op, lifecycle_op
from ops.rpc_schemas import LaunchAgentRequest, OpKind, SpawnedAgent

__all__ = [
    "local_config_in_process",
    "local_lifecycle_in_process",
    "local_spawn_in_process",
    "mock_api_keys",
    "model_catalog",
]


@pytest.fixture(autouse=True)
def local_spawn_in_process(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> Iterator[None]:
    with ExitStack() as stack:
        pool: ConnectionPool | None = None

        def _runner_pool() -> ConnectionPool:
            nonlocal pool
            if pool is None:
                pool = stack.enter_context(database.pool(max_size=4))
            return pool

        async def _in_process_forward(
            _db: object, target: str, body: LaunchAgentRequest
        ) -> SpawnedAgent:
            return await launch_agent_op(
                database,
                event_bus,
                body,
                _runner_pool(),
                catalog=model_catalog,
                authority=config_authority,
                local_machine=machine_identity.name(),
            )

        monkeypatch.setattr(_agents_creation, "forward_spawn_to_remote", _in_process_forward)

        real_lookup_role = _machines.lookup_role

        def _lookup_role(_db: Database, name: str) -> list[str]:
            if name == machine_identity.name():
                return ["gateway", "agent-runner"]
            return real_lookup_role(_db, name)

        monkeypatch.setattr(_machines, "lookup_role", _lookup_role)

        real_is_paused = _machines.is_paused

        def _is_paused(_db: Database, name: str) -> bool:
            if name == machine_identity.name():
                return False
            return real_is_paused(_db, name)

        monkeypatch.setattr(_machines, "is_paused", _is_paused)
        yield


@pytest.fixture(autouse=True)
def local_lifecycle_in_process(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
    model_catalog: ModelCatalog,
) -> Iterator[None]:
    with ExitStack() as stack:
        pool: ConnectionPool | None = None

        def _runner_pool() -> ConnectionPool:
            nonlocal pool
            if pool is None:
                pool = stack.enter_context(database.pool(max_size=4))
            return pool

        async def _in_process_lifecycle(
            _db: object, target: str, path: str, json_body: dict[str, Any]
        ) -> dict[str, Any]:
            # meiva:note
            # Forwarded lifecycle payloads are untyped dicts in unit tests.
            active_catalog = (
                cast(tuple[str, ModelCatalog], request.getfixturevalue("withdrawn_model"))[1]
                if "withdrawn_model" in request.fixturenames
                else model_catalog
            )
            return (
                await lifecycle_op(
                    database,
                    event_bus,
                    path,
                    json_body,
                    _runner_pool(),
                    catalog=active_catalog,
                )  # pyright: ignore[reportUnknownArgumentType]
            ).model_dump(mode="json")  # pyright: ignore[reportUnknownArgumentType]

        # meiva:note
        # Stand-in coroutine matches enqueue_lifecycle's runtime call contract.
        monkeypatch.setattr(_agents_forward_router, "enqueue_lifecycle", _in_process_lifecycle)  # pyright: ignore[reportUnknownArgumentType]
        yield


@pytest.fixture(autouse=True)
def local_config_in_process(
    monkeypatch: pytest.MonkeyPatch,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    """Stand-in for the config router in unit tests."""
    from ops import host_config
    from ops.cluster import rpc as cluster_rpc

    real_dispatch = cluster_rpc.dispatch_to_machine

    async def _in_process_config(
        db: Database,
        target_machine: str,
        kind: str,
        payload: dict[str, object],
        *,
        timeout_s: float | None = None,
        retries: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        if kind in ("config_read", "config_write"):
            assert target_machine == machine_identity.name(), (
                "only the local machine is simulated in-process"
            )
            if kind == "config_read":
                return host_config.config_read_op(
                    authority=config_authority,
                    local_machine=machine_identity.name(),
                    host=fake_host(),
                ).model_dump(mode="json")
            return host_config.config_write_op(
                cast("dict[str, Any]", payload["overrides"]),
                local=bool(payload.get("local", False)),
                authority=config_authority,
                local_machine=machine_identity.name(),
                host=fake_host(),
            ).model_dump(mode="json")
        return await real_dispatch(
            db,
            target_machine,
            cast("OpKind", kind),
            payload,
            timeout_s=timeout_s,
            retries=retries,
            idempotency_key=idempotency_key,
        )

    # meiva:note
    # Stand-in coroutine widens kind to str before delegating to real_dispatch.
    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _in_process_config)  # pyright: ignore[reportUnknownArgumentType]

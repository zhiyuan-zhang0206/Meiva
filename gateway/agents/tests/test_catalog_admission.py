import pytest
from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.api_contracts.agent_contract import InvalidModelConfig
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.pricing import PriceBook
from base.packages.plugins import config_registration, plugin_errors
from base.paths import meiva_home
from ops import lifecycle
from ops.rpc_schemas import RestartAgentRequest


def _catalog() -> ModelCatalog:
    return ModelCatalog(models={}, bindings={}, stops={}, prices=PriceBook({}, {}))


def test_dto_shape_does_not_construct_a_catalog() -> None:
    request = RestartAgentRequest(
        message="restarting", config_overlay={"llm_model": "test-provider-model"}
    )
    assert request.config_overlay == {"llm_model": "test-provider-model"}
    assert request.config_overlay is not None
    with pytest.raises(plugin_errors.InvalidConfigOverlay, match="not a registered model"):
        config_registration.validate_config_overlay(
            request.config_overlay, configs={}, models={}, home=meiva_home()
        )
    config_registration.validate_config_overlay(
        request.config_overlay,
        configs={},
        models={"test-provider-model": object()},
        home=meiva_home(),
    )


def test_runner_catalog_refusal_precedes_transaction(
    database: Database, event_bus: EventBus
) -> None:
    # meiva:note
    # A pool that was never opened refuses every borrow: the refusal below can only come from
    # the catalog check that runs before any transaction is acquired.
    unopened = ConnectionPool[Connection[TupleRow]]("postgresql://127.0.0.1:1/none", open=False)
    request = RestartAgentRequest(
        message="restarting", config_overlay={"llm_model": "unknown-model"}
    )
    with pytest.raises(InvalidModelConfig, match="not a registered model"):
        lifecycle.restart_blocking(
            database,
            event_bus,
            1,
            request,
            unopened,
            catalog=_catalog(),
            default_model="test-provider-model",
            home=meiva_home(),
        )

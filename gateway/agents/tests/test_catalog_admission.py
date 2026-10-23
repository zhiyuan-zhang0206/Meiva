from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

from base.api_contracts.agent_contract import InvalidModelConfig
from base.lm.catalog import ModelCatalog
from base.lm.pricing import PriceBook
from base.packages.plugins import config_registration, plugin_errors
from gateway.agents import lifecycle as gateway_lifecycle
from ops import lifecycle
from ops.rpc_schemas import RestartAgentRequest


def _catalog() -> ModelCatalog:
    return ModelCatalog(models={}, bindings={}, stops={}, prices=PriceBook({}, {}))


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    catalog=_catalog(),
                    db=MagicMock(),
                    db_pool=MagicMock(),
                    overlay_configs={},
                )
            ),
        }
    )


def test_dto_shape_does_not_construct_a_catalog() -> None:
    request = RestartAgentRequest(config_overlay={"llm_model": "test-provider-model"})
    assert request.config_overlay == {"llm_model": "test-provider-model"}
    assert request.config_overlay is not None
    with pytest.raises(plugin_errors.InvalidConfigOverlay, match="not a registered model"):
        config_registration.validate_config_overlay(request.config_overlay, configs={}, models={})
    config_registration.validate_config_overlay(
        request.config_overlay, configs={}, models={"test-provider-model": object()}
    )


def test_runner_catalog_refusal_precedes_transaction() -> None:
    pool = MagicMock()
    pool.connection.side_effect = AssertionError(
        "catalog admission must precede transaction acquisition"
    )
    request = RestartAgentRequest(config_overlay={"llm_model": "unknown-model"})
    with pytest.raises(InvalidModelConfig, match="not a registered model"):
        lifecycle.restart_blocking(pool, MagicMock(), 1, request, MagicMock(), catalog=_catalog())


@pytest.mark.asyncio
async def test_gateway_membership_refusal_is_422_before_forward() -> None:
    async def unexpected_forward(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid overlay must not be forwarded")

    request = _request()
    request.app.state.lifecycle_forward_op = unexpected_forward
    body = RestartAgentRequest(config_overlay={"llm_model": "unknown-model"})
    with pytest.raises(HTTPException) as failure:
        await gateway_lifecycle.post_agent_restart(1, request, body)
    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_gateway_preserves_runner_typed_400() -> None:
    refusal = InvalidModelConfig("provider configuration is invalid")

    async def refused_forward(*args: object, **kwargs: object) -> None:
        raise refusal

    request = _request()
    request.app.state.lifecycle_forward_op = refused_forward
    with pytest.raises(InvalidModelConfig) as failure:
        await gateway_lifecycle.post_agent_restart(1, request, RestartAgentRequest())
    assert failure.value is refusal
    assert failure.value.http_status == 400

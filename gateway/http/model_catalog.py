import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from base.lm.catalog import ModelCatalog
from gateway.http.config import GatewayAppState, gateway_state, state_as

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogLoadFailure:
    error: Exception


class ModelCatalogUnavailableError(RuntimeError):
    pass


# meiva:note
# The model catalog the app root built, or the failure that stopped it. It is declared here, not on
# `GatewayAppState`, because `CatalogLoadFailure` is this module's type and the base class lives
# below it.
class CatalogAppState(GatewayAppState):
    catalog: ModelCatalog | CatalogLoadFailure


# meiva:note
# What the app's catalog load produced for this request: the catalog, or the failure that stopped it.
def request_catalog(request: Request) -> ModelCatalog | CatalogLoadFailure:
    return state_as(request, CatalogAppState).catalog


# meiva:link gateway/http/docs/model-catalog.meiva.okf.md
def load_model_catalog(build: Callable[[], ModelCatalog]) -> ModelCatalog | CatalogLoadFailure:
    try:
        return build()
    except Exception as error:
        _log.exception("Model catalog initialization failed; gateway management remains available")
        return CatalogLoadFailure(error)


def require_catalog(result: ModelCatalog | CatalogLoadFailure) -> ModelCatalog:
    if isinstance(result, CatalogLoadFailure):
        raise ModelCatalogUnavailableError(
            "Model catalog is unavailable. Repair the provider configuration and restart the gateway."
        ) from result.error
    return result


def require_model_catalog(request: Request) -> ModelCatalog:
    return require_catalog(request_catalog(request))


def catalog_for_observation(result: ModelCatalog | CatalogLoadFailure) -> ModelCatalog | None:
    return None if isinstance(result, CatalogLoadFailure) else result


# meiva:note
# The cluster default model as of this request. The config authority is the live owner, so the
# request is the unit of work that reads it once and hands the value down.
def request_default_model(request: Request) -> str:
    value = gateway_state(request).config_authority.service_field_value("llm_model")
    if not isinstance(value, str):
        raise TypeError("llm_model must be a string")
    return value

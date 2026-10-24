import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from fastapi import Request

from base.lm.catalog import ModelCatalog

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogLoadFailure:
    error: Exception


class ModelCatalogUnavailableError(RuntimeError):
    pass


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
    return require_catalog(request.app.state.catalog)


def catalog_for_observation(result: ModelCatalog | CatalogLoadFailure) -> ModelCatalog | None:
    return None if isinstance(result, CatalogLoadFailure) else result


# meiva:note
# The cluster default model as of this request. The config authority is the live owner, so the
# request is the unit of work that reads it once and hands the value down.
def request_default_model(request: Request) -> str:
    return cast(str, request.app.state.config_authority.service_field_value("llm_model"))

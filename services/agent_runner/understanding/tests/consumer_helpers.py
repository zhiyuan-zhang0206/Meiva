# meiva:note
# Explicit resources used by the operation-root tests.

from typing import TypedDict

from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.tests.model_catalog import inert_keys


class LmWiring(TypedDict):
    catalog: ModelCatalog
    keys: ProviderKeys
    llm_override: str


def lm_wiring(catalog: ModelCatalog) -> LmWiring:
    return {"catalog": catalog, "keys": inert_keys(catalog), "llm_override": ""}


def consumer_config() -> ConfigBoot:
    return snapshot_process_config()

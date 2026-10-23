from typing import TypedDict

from base.agents.history.hierarchy.group_consumer import UnderstandingReadInputs
from base.clock import Clock
from base.config import settings
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.tests.model_catalog import inert_keys


# meiva:note
# The model wiring a consumer takes: the catalog, its inert keys and no LLM override.
class LmWiring(TypedDict):
    catalog: ModelCatalog
    keys: ProviderKeys
    llm_override: str


def lm_wiring(catalog: ModelCatalog) -> LmWiring:
    return {"catalog": catalog, "keys": inert_keys(catalog), "llm_override": ""}


def read_inputs() -> UnderstandingReadInputs:
    return UnderstandingReadInputs(
        enabled=lambda: settings.agent.understanding_enabled,
        default_model=lambda: settings.lm.llm_model,
        hierarchy_model=lambda: settings.lm.hierarchy_model,
        group_model=lambda: settings.agent.understanding_group_model,
        check_open=lambda: settings.agent.understanding_group_check_open,
        check_decay=lambda: settings.agent.understanding_group_check_decay,
        reasoning=lambda: settings.agent.understanding_group_reasoning,
        corrections=lambda: settings.agent.understanding_group_corrections,
        clock_factory=Clock.from_settings,
        timestamps_enabled=lambda: settings.general.message_timestamps,
    )

from __future__ import annotations

from dataclasses import replace
from typing import Any

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import inert_keys
from services.derived.labeler.config import LabelerConfig
from services.derived.labeler.labeler import generate_label_async
from services.derived.labeler.tests.scripted_models import SCRIPTED_MODELS


# meiva:note
# A labeler configuration whose model name selects a scripted behaviour (see `scripted_models`).
def labeler_config(model: str = "say:Fix label generation", **overrides: Any) -> LabelerConfig:
    return replace(
        LabelerConfig(labeler_model=model, labeler_max_chars=64, poll_interval_s=0.01), **overrides
    )


def labeler_db(*, database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


# meiva:note
# The model inputs the daemon's loop passes down, with the scripted-model factory as the override.
def model_inputs(catalog: ModelCatalog) -> dict[str, Any]:
    return {
        "catalog": catalog,
        "keys": inert_keys(catalog),
        "llm_override": SCRIPTED_MODELS,
        "overrides": ModelOverrides.from_pins({}),
    }


async def generate(
    agent_id: int,
    model: str,
    *,
    bus: EventBus,
    catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    prompt: str = "Fix the failing labeler",
    max_chars: int = 64,
) -> bool | None:
    return await generate_label_async(
        agent_id,
        prompt,
        labeler_config(model, labeler_max_chars=max_chars),
        labeler_db(database_gate=database_gate),
        bus,
        **model_inputs(catalog),
    )


# meiva:note
# An event bus that keeps what was published instead of sending it.
class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__(event_bus_config_from_settings())
        self.payloads: list[str] = []

    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        self.payloads.append(payload)
        return 1


# meiva:note
# An event bus whose publish raises `error`, as a bus does that is broken in a way it does not
# itself recover from.
class FailingBus(EventBus):
    def __init__(self, error: Exception) -> None:
        super().__init__(event_bus_config_from_settings())
        self.error = error

    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        raise self.error

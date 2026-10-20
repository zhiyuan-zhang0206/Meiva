"""LLM model catalog (GET /api/models) + the cluster default model
(GET/PUT /api/config/default-model).

FastAPI generates the model-picker wire contract from these schemas and the
canonical registry's vendor reference TPS metadata.
"""

from pydantic import (
    BaseModel,
)

from base.agents.model.birth_config import DefaultModelSource
from base.api_contracts.agent_contract import Harness
from base.lm.registry import ReferenceTps


class ModelPricing(BaseModel):
    """Per-million-token pricing for one model (USD)."""

    input: float  # cache-miss input rate
    cache_read: float  # cached input rate
    output: float  # output rate
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None


class ModelInfo(BaseModel):
    """Per-model metadata surfaced in the spawn picker."""

    provider: str
    context_window: int
    pricing: ModelPricing | None = None
    reference_tps: ReferenceTps | None = None
    fast_of: str | None = None
    reasoning_effort_options: list[str] | None = None
    # The model's concrete default reasoning effort — the level a spawn with no
    # explicit reasoning_effort runs at, and what the picker pre-selects.
    # None = no concrete default (effective default is the provider's own; the
    # picker then keeps a synthetic "provider default" option).
    reasoning_effort_default: str | None = None
    # The model id that replaced this one in the spawn picker, or None when the
    # model still shows. Display-only: a superseded model remains spawnable and
    # config-valid; the frontend filters it from the picker by this field.
    superseded_by: str | None = None
    # meiva:note
    # Every harness that can run this model, the built-in runtime first: the spawn form
    # keeps a model in its column only while the chosen harness appears here, so the
    # registry's per-model declaration is the single authority for that cascade.
    # meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
    harnesses: list[Harness]


class ModelsResponse(BaseModel):
    """GET /api/models — model picker source for the spawn UI."""

    providers: dict[str, list[str]]
    models: dict[str, ModelInfo]
    default: str


class DefaultModelView(BaseModel):
    """GET/PUT /api/config/default-model — the model a new agent is born on.

    `source` names where `model` came from, because the two cases behave
    differently on the next `.env` edit: `cluster` is the DB row (a deliberate
    choice through this endpoint, which outranks `.env`), `config` is the ordinary
    chain (`.env` MEIVA_MODEL, else the code default) showing through because no
    cluster choice has been made.
    """

    model: str
    source: DefaultModelSource


class DefaultModelWrite(BaseModel):
    """PUT /api/config/default-model body. `model` must be a spawnable id from the
    registry roster; anything else is rejected rather than stored."""

    model: str

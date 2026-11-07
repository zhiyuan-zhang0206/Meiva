"""LLM model catalog (GET /api/models) + the cluster default model
(GET/PUT /api/config/default-model).

FastAPI generates the model-picker wire contract from these schemas and the
canonical registry's vendor reference TPS metadata.
"""

from pydantic import BaseModel, Field

from base.api_contracts.agent_contract import (
    Harness,
    HarnessBilling,
    HarnessStatus,
    ProbedHarnessModel,
)
from base.lm.registry import ReferenceTps


class ModelPricing(BaseModel):
    """Per-million-token pricing for one model (USD)."""

    input: float  # cache-miss input rate
    cache_read: float  # cached input rate
    output: float  # output rate
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None
    cache_write_30m: float | None = None


# meiva:note
# One model and its reasoning-effort ladder probed live from an external harness CLI.
# meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
class HarnessModelOption(BaseModel):
    id: str
    reasoning_effort_options: list[str] = Field(default_factory=list)
    reasoning_effort_default: str | None = None


# meiva:note
# One harness as one machine can run it. `status` is what that machine's probe
# could prove about install, sign-in and live CLI model catalogue; `launchable`
# is this build's answer to "can a session actually be started on it there" —
# a harness can be installed and signed in and still not launchable, and the
# spawn form says so instead of letting the choice look usable.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
class HarnessAvailability(BaseModel):
    harness: Harness
    status: HarnessStatus
    reason: str
    launchable: bool
    # meiva:note
    # How this harness bills: `api` is metered per token and its rates are worth
    # showing while a model is chosen, `subscription` is not (user 2026-10-20).
    # meiva:link base/agents/docs/harness/harness.meiva.okf.md
    billing: HarnessBilling
    models: list[HarnessModelOption] = Field(default_factory=list)

    def to_probed_models(self) -> tuple[ProbedHarnessModel, ...]:
        return tuple(
            ProbedHarnessModel(
                id=item.id,
                reasoning_effort_options=tuple(item.reasoning_effort_options),
                reasoning_effort_default=item.reasoning_effort_default,
            )
            for item in self.models
        )


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


class ModelsResponse(BaseModel):
    """GET /api/models — model picker source for the spawn UI."""

    providers: dict[str, list[str]]
    models: dict[str, ModelInfo]

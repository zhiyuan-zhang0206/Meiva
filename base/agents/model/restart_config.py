# meiva:note
# Validate effective model configuration on restart overlay edits.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import LiteralString, Protocol, TypeGuard

from base.agents.model.harness_models import resolve_probed_harness_selection
from base.api_contracts.agent_contract import (
    AgentNotFound,
    Harness,
    InvalidModelConfig,
    ProbedHarnessModel,
    coerce_harness,
)
from base.lm.catalog import ModelCatalog
from base.lm.effort import validate_effort
from base.lm.registry import resolve_available_model


class RestartConfigCursor(Protocol):
    def execute(self, query: LiteralString, params: tuple[object, ...] = ..., /) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


def _is_str_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if _is_str_mapping(value) else {}


def _resolve_and_check_model(
    pins: Mapping[str, object], catalog: ModelCatalog, default_model: str
) -> str:
    raw_model = pins.get("llm_model")
    effective_model = raw_model if isinstance(raw_model, str) else default_model
    if not effective_model:
        raise InvalidModelConfig(
            "no model configured — set llm_model in cluster config or pass "
            "config.llm_model in the spawn request"
        )
    effective_model = resolve_available_model(effective_model, models=catalog.models)
    if effective_model in catalog.decision_models:
        raise InvalidModelConfig(
            f"{effective_model!r} is a decision model, not a chat model — it cannot be used as an agent "
            "or chat model; call base.lm.decision.call.decide() instead"
        )
    all_models: list[str] = [m for models in catalog.supported_models.values() for m in models]
    if effective_model not in all_models:
        raise InvalidModelConfig(
            f"unknown model {effective_model!r}. Available models: " + ", ".join(sorted(all_models))
        )
    return effective_model


def _validate_model_effort_and_harness(
    spec: object, model: str, harness: Harness, effort_str: str | None
) -> None:
    effort_levels = getattr(spec, "effort_levels", ()) or ()
    if harness is Harness.MEIVA and effort_str is not None:
        try:
            validate_effort(effort_str, effort_levels, target=model)
        except ValueError as exc:
            raise InvalidModelConfig(str(exc)) from exc


@dataclass(frozen=True)
class RestartHarnessTarget:
    harness: Harness
    probed_models: Sequence[ProbedHarnessModel] | None = None
    probed_default_model: str | None = None


def _reject_decision_model_id(candidate: str, catalog: ModelCatalog) -> None:
    if candidate in catalog.decision_models:
        raise InvalidModelConfig(
            f"{candidate!r} is a decision model, not a chat model — it cannot be used as an "
            "agent or chat model; call base.lm.decision.call.decide() instead"
        )


def _validate_external_restart_pins(
    pins: Mapping[str, object],
    *,
    catalog: ModelCatalog,
    default_model: str,
    effort_str: str | None,
    target: RestartHarnessTarget,
) -> tuple[str, str | None] | None:
    raw_model = pins.get("llm_model")
    if raw_model is not None and not isinstance(raw_model, str):
        raise InvalidModelConfig("llm_model must be a string")
    candidate = raw_model if isinstance(raw_model, str) and raw_model else None
    if candidate is not None:
        _reject_decision_model_id(candidate, catalog)
    if target.probed_models is not None:
        resolved_model, resolved_effort = resolve_probed_harness_selection(
            target.harness,
            target.probed_models,
            target.probed_default_model,
            requested_model=candidate,
            requested_effort=effort_str,
        )
        _reject_decision_model_id(resolved_model, catalog)
        return (resolved_model, resolved_effort)
    if not candidate and not default_model:
        raise InvalidModelConfig(
            "no model configured — set llm_model in cluster config or pass "
            "config.llm_model in the spawn request"
        )
    return None


def validate_restart_model_config_pins(
    pins: Mapping[str, object],
    *,
    catalog: ModelCatalog,
    default_model: str,
    target: RestartHarnessTarget | None = None,
) -> tuple[str, str | None] | None:
    effort = pins.get("reasoning_effort")
    if effort is not None and effort != "" and not isinstance(effort, str):
        raise InvalidModelConfig("reasoning_effort must be a string")
    effort_str = effort if isinstance(effort, str) and effort else None

    if target is not None and target.harness is not Harness.MEIVA:
        return _validate_external_restart_pins(
            pins,
            catalog=catalog,
            default_model=default_model,
            effort_str=effort_str,
            target=target,
        )
    effective_harness = Harness.MEIVA

    effective_model = _resolve_and_check_model(pins, catalog, default_model)
    spec = catalog.models[effective_model]
    _validate_model_effort_and_harness(spec, effective_model, effective_harness, effort_str)
    return (effective_model, effort_str)


def _resolve_target_harness(
    target: RestartHarnessTarget | None, overlay: Mapping[str, object], stored_harness: object
) -> Harness:
    if target is not None:
        return target.harness
    if "harness" in overlay:
        return coerce_harness(overlay["harness"])
    return coerce_harness(stored_harness)


# meiva:note
# Lock and check overlay > stored overlay > birth before changing either field.
#
# The caller holds a writable transaction through its overlay and restart
# writes. Provider credentials are checked on launch, not on this config edit.
# When switching onto a different harness, prior-harness model/effort pins do
# not carry over unless the caller explicitly names them in `overlay`.
def validate_restart_model_config(
    cur: RestartConfigCursor,
    agent_id: int,
    overlay: Mapping[str, object],
    *,
    catalog: ModelCatalog,
    default_model: str,
    target: RestartHarnessTarget | None = None,
) -> tuple[str, str | None] | None:
    if not {"llm_model", "reasoning_effort", "harness"}.intersection(overlay):
        return None
    cur.execute(
        "SELECT config_overlay,birth_config,harness FROM agents_meta WHERE id=%s FOR UPDATE",
        (agent_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    current_harness = coerce_harness(row[2])
    effective_harness = _resolve_target_harness(target, overlay, row[2])
    if effective_harness != current_harness:
        effective_pins: Mapping[str, object] = overlay
    else:
        effective_pins = {**_as_mapping(row[1]), **_as_mapping(row[0]), **overlay}
    resolved_target = (
        target
        if target is not None and target.harness is effective_harness
        else RestartHarnessTarget(effective_harness)
    )
    return validate_restart_model_config_pins(
        effective_pins,
        catalog=catalog,
        default_model=default_model,
        target=resolved_target,
    )

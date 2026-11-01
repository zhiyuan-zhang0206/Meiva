# meiva:note
# Validate effective model configuration on restart overlay edits.

from __future__ import annotations

from collections.abc import Mapping
from typing import LiteralString, Protocol, TypeGuard

from base.api_contracts.agent_contract import (
    AgentNotFound,
    Harness,
    InvalidModelConfig,
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
    harness_model_fn = getattr(spec, "harness_model", None)
    effort_levels = getattr(spec, "effort_levels", ()) or ()
    if harness is Harness.MEIVA:
        if effort_str is not None:
            try:
                validate_effort(effort_str, effort_levels, target=model)
            except ValueError as exc:
                raise InvalidModelConfig(str(exc)) from exc
    elif callable(harness_model_fn):
        try:
            harness_model_fn(model, harness, effort_str)
        except ValueError as exc:
            raise InvalidModelConfig(str(exc)) from exc


def validate_restart_model_config_pins(
    pins: Mapping[str, object],
    *,
    catalog: ModelCatalog,
    default_model: str,
    harness: Harness = Harness.MEIVA,
) -> None:
    effective_model = _resolve_and_check_model(pins, catalog, default_model)
    effort = pins.get("reasoning_effort")
    if effort is not None and effort != "" and not isinstance(effort, str):
        raise InvalidModelConfig("reasoning_effort must be a string")
    effort_str = effort if isinstance(effort, str) and effort else None
    spec = catalog.models[effective_model]
    _validate_model_effort_and_harness(spec, effective_model, harness, effort_str)


def _validate_effective_pins(
    pins: Mapping[str, object],
    *,
    catalog: ModelCatalog,
    default_model: str,
    harness: Harness = Harness.MEIVA,
) -> None:
    validate_restart_model_config_pins(
        pins, catalog=catalog, default_model=default_model, harness=harness
    )


# meiva:note
# Lock and check overlay > stored overlay > birth before changing either field.
#
# The caller holds a writable transaction through its overlay and restart
# writes. Provider credentials are checked on launch, not on this config edit.
def validate_restart_model_config(
    cur: RestartConfigCursor,
    agent_id: int,
    overlay: Mapping[str, object],
    *,
    catalog: ModelCatalog,
    default_model: str,
    harness: Harness | None = None,
) -> None:
    if not {"llm_model", "reasoning_effort", "harness"}.intersection(overlay):
        return
    cur.execute(
        "SELECT config_overlay,birth_config,harness FROM agents_meta WHERE id=%s FOR UPDATE",
        (agent_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    if harness is not None:
        effective_harness = harness
    elif "harness" in overlay:
        effective_harness = coerce_harness(overlay["harness"])
    else:
        effective_harness = coerce_harness(row[2])
    merged = {**_as_mapping(row[1]), **_as_mapping(row[0]), **overlay}
    _validate_effective_pins(
        merged, catalog=catalog, default_model=default_model, harness=effective_harness
    )

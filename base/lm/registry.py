# meiva:note
# `ModelTuning` field names match flat `base.config` settings fields one-for-one, with `DEFAULT_TUNING` fully populated as the shared floor and `None` serving as the topology-independent unset sentinel across `explain_setting` and `resolve_setting` (`shared-default < model-default < explicit`). `validate_models` and `validate_spec` require `attach_modalities` to be a subset of `media_types`, require `sdk_hidden_members` entries to be dotted paths under `meiva` and `sdk_doc_style` to be a plain identifier, require every spawnable model to declare `context_window`, `knowledge_cutoff`, `effort_levels`, a current price, a concrete `tuning.reasoning_effort` within `effort_levels`, and `max_output_tokens` on Anthropic-protocol bindings, and require acyclic `superseded_by` chains, valid `fast_of` standard targets, and non-spawnable `unavailable_fallback` sources pointing to spawnable replacements. `resolve_available_model` and `normalize_overlay_llm_model` settle withdrawn models to their declared fallback in one hop and return `(requested, resolved)` when rewriting an overlay.
# meiva:link base/lm/docs/model-configuration.meiva.okf.md
# meiva:note
# `ModelSpec.external_harnesses` names the external harnesses that can run a model; the built-in
# runtime (`meiva`) runs every spawnable model, so it is implied by `harnesses_for` rather than
# listed, and an empty set means the built-in loop is the only route. `validate_spec` rejects a
# value outside the `Harness` enum rather than dropping it.
# meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from typing import Any

from base.api_contracts.agent_contract import Harness
from base.lm.pricing import PriceBook


@dataclass(frozen=True)
class ModelTuning:
    auto_compact_fraction: float | None = None
    auto_compact_ceiling_tokens: int | None = None
    compact_reminder_fraction: float | None = None
    reasoning_effort: str | None = None
    claude_thinking_budget_tokens: int | None = None
    llm_retry_max_attempts: int | None = None
    llm_stream_ttft_timeout_seconds: float | None = None
    llm_stream_total_timeout_seconds: float | None = None
    llm_stream_inter_chunk_timeout_seconds: float | None = None
    agent_communication_style: str | None = None
    prompt_user_tone_enabled: bool | None = None
    prompt_prefer_sdk_enabled: bool | None = None
    prompt_keep_it_simple_enabled: bool | None = None
    prompt_output_conciseness_enabled: bool | None = None
    prompt_ui_delivery_enabled: bool | None = None
    prompt_outcome_reporting_enabled: bool | None = None
    prompt_action_caution_enabled: bool | None = None
    prompt_align_before_action_enabled: bool | None = None
    prompt_delegation_check_enabled: bool | None = None
    prompt_cross_machine_delegation_enabled: bool | None = None
    prompt_file_driven_work_enabled: bool | None = None
    prompt_temporal_awareness_enabled: bool | None = None
    prompt_invest_future_enabled: bool | None = None
    prompt_memory_behavior_enabled: bool | None = None


DEFAULT_TUNING = ModelTuning(
    auto_compact_fraction=0.4,
    auto_compact_ceiling_tokens=0,
    compact_reminder_fraction=0.3,
    reasoning_effort="",
    claude_thinking_budget_tokens=0,
    llm_retry_max_attempts=6,
    llm_stream_ttft_timeout_seconds=30.0,
    llm_stream_total_timeout_seconds=3600.0,
    llm_stream_inter_chunk_timeout_seconds=300.0,
    agent_communication_style="off",
    prompt_user_tone_enabled=True,
    prompt_prefer_sdk_enabled=True,
    prompt_keep_it_simple_enabled=True,
    prompt_output_conciseness_enabled=True,
    prompt_ui_delivery_enabled=True,
    prompt_outcome_reporting_enabled=True,
    prompt_action_caution_enabled=True,
    prompt_align_before_action_enabled=True,
    prompt_delegation_check_enabled=True,
    prompt_cross_machine_delegation_enabled=True,
    prompt_file_driven_work_enabled=True,
    prompt_temporal_awareness_enabled=True,
    prompt_invest_future_enabled=True,
    prompt_memory_behavior_enabled=True,
)


@dataclass(frozen=True)
class ReferenceTps:
    display: str
    source_url: str
    source_checked_at: str
    note: str


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    # meiva:note
    # The external harnesses that can run this model, on top of the built-in runtime. Empty is
    # the ordinary case (a vendor with no external CLI), and it is also how a spec written before
    # this field reads: nothing had ever run such a model outside the built-in loop.
    # meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
    external_harnesses: frozenset[Harness] = field(default=frozenset(), kw_only=True)
    fast_of: str | None = field(default=None, kw_only=True)
    reference_tps: ReferenceTps | None = field(default=None, kw_only=True)
    spawnable: bool = False
    unavailable_fallback: str | None = None
    superseded_by: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    knowledge_cutoff: str | None = None
    model_identity: str | None = None
    effort_levels: tuple[str, ...] | None = None
    extended_thinking_only: bool = False
    thinking_always_on: bool = False
    streaming: bool = True
    media_types: frozenset[str] = frozenset()
    attach_modalities: frozenset[str] | None = None
    sdk_hidden_members: frozenset[str] = frozenset()
    sdk_doc_style: str | None = None
    tuning: ModelTuning = field(default_factory=ModelTuning)


# meiva:note
# Every harness that can run this model, built-in runtime first: `meiva` runs every
# spawnable model, so only the external claims are declared, and their order follows the
# Harness enum rather than the order a plugin listed them in.
# meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
def harnesses_for(spec: ModelSpec) -> tuple[Harness, ...]:
    return (
        Harness.MEIVA,
        *(
            harness
            for harness in Harness
            if harness is not Harness.MEIVA and harness in spec.external_harnesses
        ),
    )


def media_types_for_model(
    model: str, *, models: Mapping[str, ModelSpec], vision_prefixes: Mapping[str, bool]
) -> frozenset[str]:
    spec = models.get(model)
    if spec is not None:
        return spec.media_types
    for prefix, vision in vision_prefixes.items():
        if model.startswith(prefix):
            return frozenset({"image"}) if vision else frozenset()
    return frozenset()


def attach_modalities_for_model(
    model: str, *, models: Mapping[str, ModelSpec], vision_prefixes: Mapping[str, bool]
) -> frozenset[str]:
    spec = models.get(model)
    if spec is not None:
        if spec.attach_modalities is not None:
            return spec.attach_modalities
        return spec.media_types
    return media_types_for_model(model, models=models, vision_prefixes=vision_prefixes)


# meiva:note
# A model's SDK-help declaration names real places: a hidden entry is a dotted path under
# `meiva`, and a style is the plain identifier a target declares with `doc_variants`. Checked
# with the rest of the catalog so a typo fails the build instead of silently hiding nothing.
def _validate_sdk_help(spec: ModelSpec, model_id: str) -> None:
    for path in sorted(spec.sdk_hidden_members):
        if path != "meiva" and not path.startswith("meiva."):
            raise RuntimeError(
                f"model {model_id!r} declares sdk_hidden_members entry {path!r} — an entry "
                f"is a dotted path under `meiva`, e.g. 'meiva.self.attach'"
            )
    style = spec.sdk_doc_style
    if style is not None and not style.isidentifier():
        raise RuntimeError(
            f"model {model_id!r} declares sdk_doc_style {style!r} — a style is a plain "
            f"identifier, the key a target declares with doc_variants"
        )


# meiva:note
# A harness the enum does not carry is refused rather than dropped: the value would
# otherwise vanish from the published per-model set and take the model out of a CLI's
# column with nothing to show for it.
# meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
def _validate_external_harnesses(spec: ModelSpec, model_id: str) -> None:
    unknown = sorted(
        repr(harness) for harness in spec.external_harnesses if not isinstance(harness, Harness)
    )
    if unknown:
        raise RuntimeError(
            f"model {model_id!r} declares external_harnesses {unknown} outside the "
            f"Harness enum {[harness.value for harness in Harness]} — a harness nothing can "
            "run is refused, never dropped"
        )


def validate_spec(
    model_id: str,
    spec: ModelSpec,
    *,
    anthropic_protocol: bool,
    prices: PriceBook,
    pending_price_models: Collection[str] = (),
) -> None:
    _validate_sdk_help(spec, model_id)
    _validate_external_harnesses(spec, model_id)
    if spec.attach_modalities is not None and not spec.attach_modalities <= spec.media_types:
        raise RuntimeError(
            f"model {model_id!r} declares attach_modalities "
            f"{sorted(spec.attach_modalities)} that are not in its media_types "
            f"{sorted(spec.media_types)} — attach rides the same message "
            f"pipeline, so it cannot accept a modality the endpoint cannot receive"
        )
    if not spec.spawnable:
        return
    missing = [
        fact
        for fact in ("context_window", "knowledge_cutoff", "effort_levels")
        if getattr(spec, fact) is None
    ]
    if missing:
        raise RuntimeError(
            f"spawnable model {model_id!r} is missing registry facts {missing} — "
            "fill them in its ModelSpec"
        )
    if model_id not in pending_price_models and prices.rates_at(model_id, input_tokens=0) is None:
        raise RuntimeError(
            f"spawnable model {model_id!r} has no current price — a catalog-priced "
            "model needs an archive entry; a plugin model needs a price in its "
            "register() call"
        )
    if not spec.tuning.reasoning_effort:
        raise RuntimeError(
            f"spawnable model {model_id!r} has no concrete reasoning_effort default — "
            f"pin one in its ModelTuning ('' provider-default is not displayable "
            f"in the spawn picker)"
        )
    levels = spec.effort_levels or ()
    if spec.tuning.reasoning_effort not in levels:
        raise RuntimeError(
            f"spawnable model {model_id!r} has reasoning_effort default "
            f"{spec.tuning.reasoning_effort!r} outside its effort_levels {levels!r} — "
            f"pin the default to one of the model's own rungs (the spawn picker "
            f"cannot render it otherwise)"
        )
    if anthropic_protocol and spec.max_output_tokens is None:
        raise RuntimeError(
            f"spawnable model {model_id!r} needs max_output_tokens — "
            f"the anthropic-protocol bindings must pin the output cap explicitly"
        )


def validate_models(
    models: Mapping[str, ModelSpec],
    *,
    prices: PriceBook,
    anthropic_protocol_by_model: Mapping[str, bool] | None = None,
) -> None:
    for tuning_field in dataclass_fields(ModelTuning):
        if getattr(DEFAULT_TUNING, tuning_field.name) is None:
            raise RuntimeError(
                f"DEFAULT_TUNING.{tuning_field.name} is None — the shared-default floor "
                f"must be fully populated (it is the last resort of resolve_setting)"
            )
    for model_id, spec in models.items():
        validate_spec(
            model_id,
            spec,
            anthropic_protocol=(
                False
                if anthropic_protocol_by_model is None
                else anthropic_protocol_by_model[model_id]
            ),
            prices=prices,
        )
    _validate_supersession_links(models)
    _validate_supersession_chains(models)
    _validate_unavailable_fallbacks(models)
    _validate_fast_links(models)


def _validate_fast_links(models: Mapping[str, ModelSpec]) -> None:
    for model_id, spec in models.items():
        if spec.fast_of is None:
            continue
        standard = models.get(spec.fast_of)
        if (
            standard is None
            or standard.fast_of is not None
            or standard.provider != spec.provider
            or not standard.spawnable
        ):
            raise RuntimeError(
                f"Fast model {model_id!r} must name a registered, spawnable standard "
                f"model from the same provider; got fast_of={spec.fast_of!r}"
            )


def _validate_supersession_links(models: Mapping[str, ModelSpec]) -> None:
    for model_id, spec in models.items():
        replacement_id = spec.superseded_by
        if replacement_id is None:
            continue
        if replacement_id == model_id:
            raise RuntimeError(
                f"model {model_id!r} lists itself as its own replacement — "
                "fix superseded_by in its provider plugin register() call"
            )
        if replacement_id not in models:
            raise RuntimeError(
                f"model {model_id!r} is superseded by {replacement_id!r}, which is "
                f"not in models — point superseded_by at a registered model id"
            )
        target = models[replacement_id]
        if not target.spawnable:
            raise RuntimeError(
                f"model {model_id!r} is superseded by {replacement_id!r}, which is "
                f"not spawnable — the replacement would never show in the picker"
            )


def _validate_supersession_chains(models: Mapping[str, ModelSpec]) -> None:
    for model_id, spec in models.items():
        seen = {model_id}
        replacement_id = spec.superseded_by
        while replacement_id is not None:
            if replacement_id in seen:
                raise RuntimeError(
                    f"superseded_by cycle from model {model_id!r} — "
                    f"point the chain at a visible model"
                )
            seen.add(replacement_id)
            replacement_id = models[replacement_id].superseded_by


def _validate_unavailable_fallbacks(models: Mapping[str, ModelSpec]) -> None:
    for model_id, spec in models.items():
        fallback_id = spec.unavailable_fallback
        if fallback_id is None:
            continue
        if spec.spawnable:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} remains spawnable — "
                "remove it from the picker before assigning unavailable_fallback"
            )
        if fallback_id not in models:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} falls back to {fallback_id!r}, "
                "which is not in models"
            )
        if not models[fallback_id].spawnable:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} falls back to {fallback_id!r}, "
                "which is not spawnable"
            )


def resolve_available_model(model: str, *, models: Mapping[str, ModelSpec]) -> str:
    spec = models.get(model)
    return spec.unavailable_fallback if spec and spec.unavailable_fallback else model


def normalize_overlay_llm_model(
    config: dict[str, object], *, models: Mapping[str, ModelSpec]
) -> tuple[str, str] | None:
    requested = config.get("llm_model")
    if not isinstance(requested, str):
        return None
    resolved = resolve_available_model(requested, models=models)
    if resolved == requested:
        return None
    config["llm_model"] = resolved
    return requested, resolved


@dataclass(frozen=True)
class ResolvedSetting:
    setting: str
    value: Any
    source: str
    shared_default: Any
    model_default: Any | None
    explicit_value: Any | None


def tuning_field_names() -> tuple[str, ...]:
    return tuple(f.name for f in dataclass_fields(ModelTuning))


def explain_setting(
    setting: str, *, model: str, explicit: Any, models: Mapping[str, ModelSpec]
) -> ResolvedSetting:
    floor = getattr(DEFAULT_TUNING, setting)
    spec = models.get(model)
    tuned = getattr(spec.tuning, setting) if spec is not None else None
    if explicit is not None:
        return ResolvedSetting(setting, explicit, "explicit", floor, tuned, explicit)
    if tuned is not None:
        return ResolvedSetting(setting, tuned, "model-default", floor, tuned, None)
    return ResolvedSetting(setting, floor, "shared-default", floor, tuned, None)


def resolve_setting(
    setting: str, *, model: str, models: Mapping[str, ModelSpec], explicit: Any
) -> Any:
    return explain_setting(setting, model=model, models=models, explicit=explicit).value

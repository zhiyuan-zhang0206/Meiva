# meiva:note
# The model call behind one `meiva.understand` target, decided from values: with which build
# options the routed model is built, how a file's suffix classifies it, and which configuration is
# refused before any model is built. Which model answers is `routing.py`; what content it receives
# is `content.py`. Every model goes through the same provider factory as every other LLM path.
# The shell in `answer.py` reads the files, builds the model and invokes it.
# meiva:link meiva/understand/docs/understand.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

from base.lm.attach.constants import ATTACH_MEDIA_MIME
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort, validate_effort
from meiva.understand.errors import UnderstandError
from meiva.understand.routing import ROUTE_ORDER

__all__ = [
    "BuildOptions",
    "MediaSettings",
    "ModelCall",
    "media_mime",
    "plan_call",
    "plan_text_call",
    "supported_media_suffixes",
]

# meiva:note
# Gemini media-resolution vocabulary (mapped by the gemini provider branch); validated here so an
# invalid setting fails fast with the setting's name before any model call.
_MEDIA_RESOLUTION_LEVELS = frozenset({"low", "medium", "high"})


# meiva:note
# The configured Gemini media knobs: resolution, the thinking level the default effort selects,
# and an optional endpoint override for a self-hosted relay. They apply only to a media call a
# Gemini model answers.
@dataclass(frozen=True)
class MediaSettings:
    resolution: str
    thinking_level: str | None
    base_url: str | None


# meiva:note
# What the provider factory is asked to build the chat model with. The media fields stay None
# unless a Gemini model answers a media call, which is what the factory defaults them to.
@dataclass(frozen=True)
class BuildOptions:
    reasoning_effort: str | ReasoningEffort
    timeout: float
    media_resolution: str | None
    media_thinking_level: str | None
    base_url: str | None


# meiva:note
# One model call: the model, how it is built, how a failure labels itself, and the usage source
# of the `llm_usage` row (`understand` for text, `understand.<type>` for media, named by the
# richest type of the content).
@dataclass(frozen=True)
class ModelCall:
    model: str
    desc: str
    usage_source: str
    build: BuildOptions


# meiva:note
# A suffix maps to a media MIME type, or None when the file is read as UTF-8 text instead: so
# .txt, .md, .csv, .json, .py and extensionless files all flow through the text path.
def media_mime(suffix: str) -> str | None:
    return ATTACH_MEDIA_MIME.get(suffix.lower())


def supported_media_suffixes() -> list[str]:
    return sorted(ATTACH_MEDIA_MIME)


# meiva:note
# The call over a text-only content list: `effort` rides the build options, and the exact
# per-model validation in the factory rejects unsupported graded values.
def plan_text_call(text_model: str, timeout: float, effort: str | ReasoningEffort) -> ModelCall:
    return ModelCall(
        model=text_model,
        desc=f"{text_model}, text",
        usage_source="understand",
        build=BuildOptions(
            reasoning_effort=effort,
            timeout=timeout,
            media_resolution=None,
            media_thinking_level=None,
            base_url=None,
        ),
    )


# meiva:note
# The call `model` (chosen by `route`) makes over content of `types`; the types label a failure.
# All-text content is a text call. A media call on a Gemini model carries the Gemini knobs; on
# any other model `effort` rides the build options like a text call, validated by the factory
# against the model's own levels.
#
# Raises:
#     UnderstandError: a Gemini media call's configured resolution is not one of low/medium/high,
#         or the model declares no effort vocabulary.
#     ValueError: `effort` is not one of the Gemini model's levels.
def plan_call(
    catalog: ModelCatalog,
    model: str,
    types: frozenset[str],
    *,
    timeout: float,
    effort: str | ReasoningEffort,
    settings: MediaSettings,
) -> ModelCall:
    richest = next(t for t in ROUTE_ORDER if t in types)
    if richest == "text":
        return plan_text_call(model, timeout, effort)
    if catalog.provider_key_of(model) == "gemini":
        build = _gemini_media_build(
            catalog, model, timeout=timeout, effort=effort, settings=settings
        )
    else:
        build = BuildOptions(
            reasoning_effort=effort,
            timeout=timeout,
            media_resolution=None,
            media_thinking_level=None,
            base_url=None,
        )
    return ModelCall(
        model=model,
        desc=f"{model}, {'+'.join(t for t in ROUTE_ORDER if t in types)}",
        usage_source=f"understand.{richest}",
        build=build,
    )


# meiva:note
# A Gemini media call's build options. Graded efforts must be exact model-supported values: the
# default `max` selects the configured thinking level, and an explicit `none` selects the
# model's lowest level because Gemini cannot turn thinking off.
def _gemini_media_build(
    catalog: ModelCatalog,
    model: str,
    *,
    timeout: float,
    effort: str | ReasoningEffort,
    settings: MediaSettings,
) -> BuildOptions:
    if settings.resolution not in _MEDIA_RESOLUTION_LEVELS:
        raise UnderstandError(
            f"invalid MEIVA_UNDERSTAND_MEDIA_RESOLUTION={settings.resolution!r} — "
            f"must be one of {sorted(_MEDIA_RESOLUTION_LEVELS)}"
        )
    thinking_level = settings.thinking_level
    if effort != ReasoningEffort.MAX:
        levels = catalog.models[model].effort_levels
        if not levels:
            raise UnderstandError(f"model {model!r} does not declare a reasoning-effort vocabulary")
        thinking_level = (
            levels[0]
            if effort == ReasoningEffort.NONE
            else validate_effort(effort, levels, target=model)
        )
    return BuildOptions(
        reasoning_effort=effort,
        timeout=timeout,
        media_resolution=settings.resolution,
        media_thinking_level=thinking_level,
        base_url=settings.base_url,
    )

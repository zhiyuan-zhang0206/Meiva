# meiva:note
# The model call behind one `meiva.understand` target, decided from values: which model answers,
# with which build options, how a file's suffix classifies it, and which configuration is refused
# before any model is built. The provider split by modality is configuration: the text model
# handles literal strings and text files, the media model handles binary media, and the media
# model goes through the same provider factory as every other LLM path. The shell in `answer.py`
# builds the model and invokes it.
# meiva:link meiva/understand/docs/understand.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

from base.lm.attach.constants import ATTACH_MEDIA_MIME
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort, validate_effort
from meiva.understand.errors import UnderstandError

__all__ = [
    "INLINE_MAX_BYTES",
    "BuildOptions",
    "MediaSettings",
    "ModelCall",
    "check_inline_size",
    "media_mime",
    "plan_media_call",
    "plan_text_call",
    "supported_media_suffixes",
]

# meiva:note
# Gemini inline-media cap (API docs: 20MB inline), the default media provider's limit. It applies
# only to binary media read as bytes; the text path has no such cap.
INLINE_MAX_BYTES = 20 * 1024 * 1024

# meiva:note
# Gemini media-resolution vocabulary (mapped by the gemini provider branch); validated here so an
# invalid setting fails fast with the setting's name before any model call.
_MEDIA_RESOLUTION_LEVELS = frozenset({"low", "medium", "high"})


# meiva:note
# The configured media knobs: Gemini resolution, the thinking level the default effort selects,
# and an optional endpoint override for a self-hosted relay.
@dataclass(frozen=True)
class MediaSettings:
    resolution: str
    thinking_level: str | None
    base_url: str | None


# meiva:note
# What the provider factory is asked to build the chat model with. The media fields stay None on
# the text path, which is what the factory defaults them to.
@dataclass(frozen=True)
class BuildOptions:
    reasoning_effort: str | ReasoningEffort
    timeout: float
    media_resolution: str | None
    media_thinking_level: str | None
    base_url: str | None


# meiva:note
# One model call: the model, how it is built, how a failure labels itself, and the usage source
# of the `llm_usage` row.
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
# A media file over the inline cap is refused before any model is built.
def check_inline_size(size: int) -> None:
    if size > INLINE_MAX_BYTES:
        raise UnderstandError(
            f"File size {size:,} bytes exceeds the {INLINE_MAX_BYTES:,} byte "
            "media inline cap (the default Gemini media provider's limit)"
        )


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
# The call over a content list with media. The media part shape is Gemini-specific, so a
# non-Gemini model fails here with a clear error instead of crashing at invoke time. Graded
# efforts must be exact model-supported values: the default `max` selects the configured thinking
# level, and an explicit `none` selects the model's lowest level because Gemini cannot turn
# thinking off.
#
# Raises:
#     UnderstandError: the media model is not a Gemini model, the configured resolution is not
#         one of low/medium/high, or the model declares no effort vocabulary.
#     ValueError: `effort` is not one of the model's levels.
def plan_media_call(
    catalog: ModelCatalog,
    model: str,
    *,
    mime: str,
    timeout: float,
    effort: str | ReasoningEffort,
    settings: MediaSettings,
) -> ModelCall:
    if catalog.provider_key_of(model) != "gemini":
        raise UnderstandError(
            f"media model {model!r} is not supported by meiva.understand — the "
            "media path (image/video/audio/PDF) currently supports Gemini "
            "models only (their media wire format is the only one the factory "
            "clients accept). Set MEIVA_UNDERSTAND_MEDIA_MODEL to a gemini-* model "
            "or wait for a second media provider."
        )
    if settings.resolution not in _MEDIA_RESOLUTION_LEVELS:
        raise UnderstandError(
            f"invalid MEIVA_UNDERSTAND_MEDIA_RESOLUTION={settings.resolution!r} — "
            f"must be one of {sorted(_MEDIA_RESOLUTION_LEVELS)}"
        )
    thinking_level = settings.thinking_level
    if effort != ReasoningEffort.MAX:
        levels = catalog.models[model].effort_levels
        if not levels:
            raise UnderstandError(
                f"media model {model!r} does not declare a reasoning-effort vocabulary"
            )
        thinking_level = (
            levels[0]
            if effort == ReasoningEffort.NONE
            else validate_effort(effort, levels, target=model)
        )
    return ModelCall(
        model=model,
        desc=f"{model}, mime={mime}",
        usage_source="understand.media",
        build=BuildOptions(
            reasoning_effort=effort,
            timeout=timeout,
            media_resolution=settings.resolution,
            media_thinking_level=thinking_level,
            base_url=settings.base_url,
        ),
    )

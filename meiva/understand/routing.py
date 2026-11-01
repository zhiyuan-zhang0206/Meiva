# meiva:note
# Which model answers a target: each content type (text, image, pdf, audio, video) has its own
# configured model, and a target whose files mix types goes to the one model that accepts all of
# them. Decided from values (the catalog, the configured models, the types present) before any
# model is built. The media types are the catalog's `ModelSpec.media_types` vocabulary.
# meiva:link meiva/understand/docs/understand.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

from base.lm.catalog import ModelCatalog
from base.lm.registry import media_types_for_model
from meiva.understand.errors import UnderstandError

__all__ = ["ROUTE_ORDER", "UnderstandModels", "model_setting", "route"]

# meiva:note
# The content types in the order their configured models are tried as the one model of a mixed
# target: the richest modality first, text last. The media types are the `ModelSpec.media_types`
# vocabulary.
ROUTE_ORDER: tuple[str, ...] = ("video", "audio", "pdf", "image", "text")


# meiva:note
# The configured model of each content type (`understand_<type>_model`).
@dataclass(frozen=True)
class UnderstandModels:
    text: str
    image: str
    pdf: str
    audio: str
    video: str

    def of(self, content_type: str) -> str:
        match content_type:
            case "text":
                return self.text
            case "image":
                return self.image
            case "pdf":
                return self.pdf
            case "audio":
                return self.audio
            case "video":
                return self.video
            case _:
                raise ValueError(f"unknown understand content type {content_type!r}")


# meiva:note
# The environment name of the setting that configures `content_type`'s model, the name a
# refusal points the operator at.
def model_setting(content_type: str) -> str:
    return f"MEIVA_UNDERSTAND_{content_type.upper()}_MODEL"


def _accepted(catalog: ModelCatalog, model: str) -> frozenset[str]:
    return media_types_for_model(
        model,
        models=catalog.models,
        vision_prefixes={prefix: binding.vision for prefix, binding in catalog.bindings.items()},
    )


# meiva:note
# The one model that answers content of `types`. Every present media type's configured model must
# accept its own type in the catalog (a configuration error otherwise, named by its setting).
# Candidates are the configured models of the present types in `ROUTE_ORDER`; the first that
# accepts every present media type answers. Text needs no capability, so an all-text target
# goes to the text model.
#
# Raises:
#     UnderstandError: a configured model does not accept its own type, or no candidate accepts
#         every present media type.
def route(catalog: ModelCatalog, models: UnderstandModels, types: frozenset[str]) -> str:
    media = types - {"text"}
    present = [t for t in ROUTE_ORDER if t in types]
    for content_type in present:
        model = models.of(content_type)
        if content_type in media and content_type not in _accepted(catalog, model):
            raise UnderstandError(
                f"{model_setting(content_type)}={model!r} does not accept {content_type} input "
                "— configure a model whose catalog entry lists it"
            )
    for content_type in present:
        model = models.of(content_type)
        if media <= _accepted(catalog, model):
            return model
    configured = ", ".join(f"{model_setting(t)}={models.of(t)!r}" for t in present)
    raise UnderstandError(
        f"no configured understand model accepts {sorted(media)} together ({configured}) — "
        "configure one of them to a model that accepts every type, or ask about these files "
        "in separate targets"
    )

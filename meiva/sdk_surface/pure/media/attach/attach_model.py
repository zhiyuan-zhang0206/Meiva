# meiva:note
# What a model can receive and which SDK members that gates, decided over the model catalog its
# caller read: the media modalities of a model (after any withdrawal fallback), the dotted `meiva`
# members unavailable for it and the model an agent actually runs as. The registration shell
# (`attachment_transport.py`) and the help renderer read the agent's configured model and catalog;
# these functions only choose.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from base.lm.catalog import ModelCatalog
from base.lm.registry import attach_modalities_for_model, resolve_available_model
from meiva.sdk_surface.pure.media.attach.attach_plan import gated_members, unavailable_reason


# meiva:note
# The media modalities `model` receives natively, judged on the model that will actually run: a
# withdrawn id is gated as its fallback.
def modalities_for(model: str, catalog: ModelCatalog) -> frozenset[str]:
    return attach_modalities_for_model(
        resolve_available_model(model, models=catalog.models),
        models=catalog.models,
        vision_prefixes={prefix: binding.vision for prefix, binding in catalog.bindings.items()},
    )


# meiva:note
# Dotted ``meiva`` member paths unavailable for `model`'s media capability: ``meiva.self.attach``
# on a text-only model (user ruling 2026-08-28). The help() renderer hides these from the SDK
# docs; empty for a media-capable model.
def media_gated_members(model: str, *, catalog: ModelCatalog) -> frozenset[str]:
    return gated_members(unavailable_reason(model, modalities_for(model, catalog)))


# meiva:note
# The model an agent runs as, given the id its settings configure: capability gates judge the
# model that will actually run, so a withdrawn id is gated as its fallback.
def running_model(configured: str, *, catalog: ModelCatalog) -> str:
    return resolve_available_model(configured, models=catalog.models)

"""Register media for delivery through the exec turn's state update.

`meiva.self.attach` appends each registration to `meiva.state_update["attach"]`; the exec node takes
the entries out of the delta it commits, validates them, parks them in the `attach` channel of the
checkpointed state and turns them into one media message appended right after the exec output of the
registering turn (user ruling 2026-08-26). The SDK keeps no buffer of its own.
"""

from __future__ import annotations

import inspect
import re as _re
import stat
from pathlib import Path
from typing import Any

from base.lm.catalog import ModelCatalog
from base.lm.registry import attach_modalities_for_model, resolve_available_model
from meiva.sdk_surface import process_context
from meiva.sdk_surface.pure.media.attach_plan import (
    ATTACH_CHANNEL,
    AttachFacts,
    appended_channel,
    gated_members,
    refusal,
    resolve_in,
    unavailable_reason,
)
from meiva.sdk_surface.validation import coerce_str


def format_signature(target: object) -> str:
    if not callable(target):
        return "(...)"
    try:
        sig = str(inspect.signature(target))
    except (ValueError, TypeError):
        return "(...)"
    sig = _re.sub(r"(: |-> )'([^']*)'(?=[,)= ]|$)", r"\1\2", sig)
    sig = _re.sub(r"<(\w+(?:\.\w+)+): [^<>]*>", r"\1", sig)
    return sig.replace("typing.", "")


# meiva:note
# Dotted ``meiva`` member paths unavailable for `model`'s media capability: ``meiva.self.attach``
# on a text-only model (user ruling 2026-08-28). The help() renderer hides these from the SDK
# docs; empty for a media-capable model.
def media_gated_members(model: str, *, catalog: ModelCatalog) -> frozenset[str]:
    return gated_members(unavailable_reason(model, _modalities(model, catalog)))


# meiva:note
# The agent's configured model, resolved through any withdrawal fallback: capability gates
# judge the model that will actually run, so a withdrawn id is gated as its fallback.
def current_model() -> str:
    from meiva.sdk_surface import settings as _settings

    return resolve_available_model(
        _settings.agent_setting("llm_model"), models=_settings.model_catalog().models
    )


def _modalities(model: str, catalog: ModelCatalog) -> frozenset[str]:
    return attach_modalities_for_model(
        resolve_available_model(model, models=catalog.models),
        models=catalog.models,
        vision_prefixes={prefix: binding.vision for prefix, binding in catalog.bindings.items()},
    )


# meiva:note
# One media registration decided over explicit inputs: validate the request and return the
# entry to record, or raise the refusal `attach_plan` chose. `base` is the directory a relative
# `path` resolves against, `model` the agent's resolved model and `in_turn` whether this
# process runs an exec turn on its own identity (a borrowed lease never does). The only reads
# are the file facts of the resolved path.
def plan_media(
    path: str | Path,
    *,
    label: str | None,
    base: Path,
    catalog: ModelCatalog,
    model: str,
    in_turn: bool,
) -> tuple[str, str | None]:
    modalities = _modalities(model, catalog)
    if reason := unavailable_reason(model, modalities):
        raise RuntimeError(
            f"meiva.self.attach is unavailable: {reason}; switch to a vision-capable model"
        )
    path = coerce_str(path, "path", allow_types=(Path,))
    label = coerce_str(label, "label", allow_none=True)
    resolved = resolve_in(base, Path(path).expanduser()).resolve()
    try:
        st = resolved.stat()
    except OSError:
        is_file = False
        size = 0
    else:
        is_file = stat.S_ISREG(st.st_mode)
        size = st.st_size if is_file else 0
    refused = refusal(
        AttachFacts(
            model=model,
            modalities=modalities,
            in_turn=in_turn,
            shown_path=str(path),
            resolved_path=str(resolved),
            is_file=is_file,
            suffix=resolved.suffix,
            size=size,
            label=label,
        )
    )
    if refused is not None:
        raise refused.error(refused.message)
    return str(resolved), label


# meiva:note
# Append one registration to the exec turn's state-update dict, the channel the exec node takes
# the entries out of. `update` is that dict (anything else is a framework bug and raises).
def record_media(update: object, entry: tuple[str, str | None]) -> None:
    state: Any = update
    if not isinstance(update, dict):
        raise TypeError(
            f"meiva.state_update must stay a dict, got {type(update).__name__} (attachment)"
        )
    state[ATTACH_CHANNEL] = appended_channel(state.get(ATTACH_CHANNEL, []), *entry)


# meiva:note
# `meiva.self.attach` as the process sees it: read the catalog, the model, the turn and the
# state-update slot once, then register. `base` is the caller's workspace directory.
def register(path: str | Path, *, label: str | None, base: Path) -> None:
    from meiva.sdk_surface import settings as sdk_settings

    bound = process_context.ambient_context()
    borrowed = bound is not None and bound.identity is not None and bound.identity.lease is not None
    in_turn = process_context.in_exec_turn() and not borrowed
    entry = plan_media(
        path,
        label=label,
        base=base,
        catalog=sdk_settings.model_catalog(),
        model=current_model(),
        in_turn=in_turn,
    )
    record_media(process_context.get_state_update(), entry)

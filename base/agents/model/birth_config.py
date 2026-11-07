# meiva:note
# Birth-frozen per-agent config — resolution at spawn, replay for life.
# Neither meiva nor external harnesses provide a fallback default model. At birth,
# llm_model must be explicitly provided in config_overlay, or inherited from the
# spawning parent agent (Case A) or fork source (Case B).

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from base.config import frozen_field_names
from base.config.service_read import ConfigAuthority
from base.host.boundary.untyped import object_mapping


def _json_safe(value: object) -> object:
    return str(value) if isinstance(value, Path) else value


# meiva:note
# The frozen-field map to persist on a newly born agent's row.
def resolve_birth_config(
    overlay: Mapping[str, object] | None = None,
    *,
    authority: ConfigAuthority,
    inherited: Mapping[str, object] | None = None,
) -> dict[str, object]:
    stamped: dict[str, object] = dict(inherited or {})
    overlay_keys = set(overlay or {})
    pending = [n for n in frozen_field_names() if n not in overlay_keys and n not in stamped]
    if "llm_model" in pending:
        raise ValueError("no model configured — llm_model must be explicitly provided or inherited")
    if not pending:
        return stamped
    values = object_mapping(authority.current_field_values())
    for name in sorted(pending):
        stamped[name] = _json_safe(values[name])
    return stamped


HARNESS_MODEL_SELECTION_KEY = "harness_model_selection"
HARNESS_MODEL_SELECTION_VERSION = 1


# meiva:note
# Before execution selection existed, external agents stored a native history model in the same
# fields. Only a stamped birth opts those pins into CLI execution; old sessions keep their CLI
# defaults on resume. Unknown stored versions fail rather than reinterpret historical choices.
# meiva:link base/agents/docs/harness/harness-models.meiva.okf.md
def uses_harness_model_selection(birth_config: Mapping[str, object] | None) -> bool:
    if birth_config is None or HARNESS_MODEL_SELECTION_KEY not in birth_config:
        return False
    version = birth_config[HARNESS_MODEL_SELECTION_KEY]
    if type(version) is not int or version != HARNESS_MODEL_SELECTION_VERSION:
        raise ValueError(f"unsupported harness model selection version: {version!r}")
    return True

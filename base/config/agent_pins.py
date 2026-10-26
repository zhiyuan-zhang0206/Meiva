"""An agent's framework config pins: its two stored maps merged into the flat pin map.

Many agents' turns share one host process, so a `per_agent=True` field cannot live in the
process-global `settings` singleton alone. The host resolves each agent's pins

    config_overlay (agents_meta)  >  birth_config (frozen fields)  >  cluster default (live)

and builds the agent's `AgentSlices` from them (`base/host/env/agent_slices.py`): a pin wins for
the agent, an unpinned field falls through to the live singleton, so a cluster-default edit keeps
reaching agents as the `lifecycle: live` contract promises. Pin values stay raw: validation
happened before they reached agents_meta, and reading the stored value must not silently coerce
it a second time.

Scope: framework `Settings` fields only. Plugin-scope config has the same problem and its own
resolution (`base/packages/plugins/config_view.py`).

Enforcement: `scripts/lint/turn_scoped_config.py` forbids reading a `per_agent` field through the
bare singleton from the turn-scoped packages — those reads come from the agent's slices.
"""

from __future__ import annotations

from collections.abc import Mapping

import base.config.config_registry as _config_registry
from base.config.config_registry import Lifecycle, per_agent_field_names, schema_extra


# meiva:note
# The per-agent lifecycle class of a `per_agent=True` field.
def field_lifecycle(name: str) -> Lifecycle:
    extra = schema_extra(_config_registry.fields()[name].info)
    if extra.get("per_agent") is not True:
        raise KeyError(f"config field {name!r} is not per_agent — it has no lifecycle class")
    lifecycle = extra.get("lifecycle")
    if lifecycle == "frozen":
        return "frozen"
    if lifecycle == "live":
        return "live"
    raise RuntimeError(f"config field {name!r} has invalid lifecycle {lifecycle!r}")


# meiva:note
# Per-agent field names whose value is resolved once at spawn and replayed for
# the agent's life — the set `base/agents/model/birth_config.py` stamps into
# `agents_meta.birth_config`.
def frozen_field_names() -> set[str]:
    return {n for n in per_agent_field_names() if field_lifecycle(n) == "frozen"}


# meiva:note
# Per-agent field names re-read from current cluster config at every process
# start (absent an explicit overlay). The complement of `frozen_field_names`.
def live_field_names() -> set[str]:
    return {n for n in per_agent_field_names() if field_lifecycle(n) == "live"}


def resolve_agent_config_pins(
    config_overlay: Mapping[str, object] | None,
    birth_config: Mapping[str, object] | None,
) -> dict[str, object]:
    """Merge an agent's two stored config maps into the flat pin map.

    birth_config first, config_overlay on top. Only framework `Settings` fields are kept:
    plugin-scope overlay keys (dotted `plugin.field` or plugin-owned flat keys)
    are not pinnable through this view yet and are dropped here — the hosted
    host applies them via the plugin path separately.

    Unknown keys are dropped rather than raised: the maps were validated at
    write time (`validate_config_overlay`), so an unknown key here means the
    field was deleted from Settings after the overlay was stored. Reading old
    stored configuration does not restore a deleted field.
    """
    fields = _config_registry.fields()
    pins: dict[str, object] = {}
    for source in (birth_config, config_overlay):
        if not source:
            continue
        for key, value in source.items():
            if key in fields:
                pins[key] = value
    return pins

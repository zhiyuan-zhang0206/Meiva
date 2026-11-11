from collections.abc import Mapping
from typing import NamedTuple

from base.api_contracts.agent_contract import Harness
from base.packages.plugins.plugin_errors import InvalidConfigOverlay

__all__ = ["COLUMN_OVERLAY_KEYS", "OverlayColumns", "split_overlay_columns"]


# meiva:note
# Overlay keys that name a column of the agent's row instead of a config field. `harness` is the
# only one: the runtime loop an agent is born on, switchable on restart. An overlay names it
# beside the config fields, at spawn and at restart alike, so an agent's harness is chosen the
# way its model is. The value is never a config field: it has no cluster default, no `.env`
# alias and no copy in `config_overlay` or `birth_config`. `agents_meta.harness` stays the one
# place it is stored, and every reader of an overlay takes it out through
# `split_overlay_columns` before the remaining keys are validated or merged.
# meiva:link base/agents/docs/harness/harness.md
COLUMN_OVERLAY_KEYS: frozenset[str] = frozenset({"harness"})


# meiva:note
# An overlay's row-column keys, parsed, and the config fields left over.
class OverlayColumns(NamedTuple):
    harness: Harness | None
    fields: dict[str, object]


# meiva:note
# The one reading of an overlay's row-column keys, shared by spawn and restart. A missing key is
# None (the caller's own default: a plain spawn runs native, a fork keeps its source's harness, a
# restart keeps the stored one). A present key must name a `Harness`: an unknown value or a
# non-string, including an explicit null, is refused instead of falling back to the default.
def split_overlay_columns(overlay: Mapping[str, object] | None) -> OverlayColumns:
    fields = {k: v for k, v in (overlay or {}).items() if k not in COLUMN_OVERLAY_KEYS}
    if overlay is None or "harness" not in overlay:
        return OverlayColumns(None, fields)
    raw = overlay["harness"]
    legal = ", ".join(harness.value for harness in Harness)
    if not isinstance(raw, str):
        raise InvalidConfigOverlay(
            f"overlay key 'harness' value {raw!r} is not a harness name; legal values: {legal}"
        )
    try:
        return OverlayColumns(Harness(raw), fields)
    except ValueError:
        raise InvalidConfigOverlay(
            f"overlay key 'harness' value {raw!r} is not a harness; legal values: {legal}"
        ) from None

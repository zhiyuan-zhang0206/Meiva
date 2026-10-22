from __future__ import annotations

from meizel.state.ambient_state import allowlist as allow
from meizel.state.ambient_state.rootrule import FromSettingsRule

RULE = FromSettingsRule(
    rule="ambient-endpoint",
    class_name=allow.ENDPOINT_HANDLE_CLASS,
    modules=allow.ENDPOINT_HANDLE_MODULES,
    registry="ENDPOINT_PACKAGES",
)
AMBIENT_ENDPOINT = RULE.rule
FIX = (
    f"take the daemon's endpoint (or the `{allow.ENDPOINT_HANDLE_CLASS}` table) from the composition "
    f"root named in ENDPOINT_PACKAGES; only the root calls `{RULE.site}()`"
)
hits = RULE.hits

from __future__ import annotations

from meizel.state.ambient_state import allowlist as allow
from meizel.state.ambient_state.rootrule import FromSettingsRule

RULE = FromSettingsRule(
    rule="ambient-bus",
    class_name=allow.BUS_HANDLE_CLASS,
    modules=allow.BUS_HANDLE_MODULES,
    registry="BUS_PACKAGES",
)
AMBIENT_BUS = RULE.rule
FIX = (
    f"take the `{allow.BUS_HANDLE_CLASS}` from the composition root named in BUS_PACKAGES (a daemon "
    f"root, or `request.app.state.bus` in the gateway); only the root calls `{RULE.site}()`"
)
hits = RULE.hits
builds = RULE.builds
package_of = RULE.package_of

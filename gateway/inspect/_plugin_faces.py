# meiva:note
# Which shipped plugins the gateway's inspector surfaces load faces from.
#
# The gateway never runs plugin code of its own beyond a plugin's data faces (`metrics.py`,
# `inspector.py`); a face is loaded for a builtin plugin only while that plugin is ENABLED
# (`plugins_config`), consulted per request, so `meiva plugins disable` takes effect without a gateway
# restart. Metrics and widgets use the same rule. (The Grafana dashboard supply is a different
# surface: it renders panels for every shipped plugin so a disabled plugin's history stays visible.)
#
# The enable state is read once per request by `enabled_plugin_names`; which face files that selects
# is `enabled_face_files`, a function of the plugin directory and that set of names.

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from base.packages.plugins import enable_config

# meiva:note
# The shipped-plugin directory: every ENABLED builtin plugin dir with a `metrics.py` or an
# `inspector.py` is part of the in-process registries. External plugin code is not imported by
# the gateway.
SHIPPED_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "meiva_builtins" / "plugins"


# meiva:note
# The names of the plugins enabled on this machine right now (read from `plugins_config`).
def enabled_plugin_names() -> frozenset[str]:
    installed = enable_config.installed_plugin_dirs()
    config = enable_config.load_for_runtime(set(installed))
    return frozenset(name for name, entry in config.plugins.items() if entry.enabled)


def enabled_face_files(plugins_dir: Path, filename: str, enabled: Collection[str]) -> list[Path]:
    """`filename` of every ENABLED plugin directory under `plugins_dir` that ships it, sorted by
    plugin name."""
    if not plugins_dir.is_dir():
        return []
    return [
        face
        for plugin_dir in sorted(plugins_dir.iterdir())
        if plugin_dir.name in enabled and (face := plugin_dir / filename).is_file()
    ]

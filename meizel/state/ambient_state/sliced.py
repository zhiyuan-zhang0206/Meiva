from __future__ import annotations

import ast

from meizel.state.ambient_state import allowlist as allow
from meizel.state.ambient_state.scan import Hit

SETTINGS_READ = "settings-read"
FIX = (
    "this package receives its configuration as constructor arguments (its slices in "
    "`config.py`); only the composition root named in SLICED_PACKAGES reads `settings` and builds them"
)


def package_of(rel: str) -> str | None:
    """The governed package directory holding this module, if any."""
    return next((pkg for pkg in allow.SLICED_PACKAGES if rel.startswith(f"{pkg}/")), None)


def _is_config(module: str | None) -> bool:
    return module is not None and any(
        module == cfg or module.startswith(f"{cfg}.") for cfg in allow.CONFIG_MODULES
    )


def _imported_readers(node: ast.Import | ast.ImportFrom) -> list[str]:
    """The names of the global configuration one import statement reaches."""
    if isinstance(node, ast.ImportFrom) and node.level == 0:
        if _is_config(node.module):
            return [a.name for a in node.names if a.name in allow.CONFIG_READERS]
        for cfg in allow.CONFIG_MODULES:
            parent, _, leaf = cfg.rpartition(".")
            if parent and node.module == parent:
                return [cfg for a in node.names if a.name == leaf]
    elif isinstance(node, ast.Import):
        return [a.name for a in node.names if _is_config(a.name)]
    return []


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every read of the global configuration in a governed, non-root module."""
    package = package_of(rel)
    if package is None or rel in allow.SLICED_PACKAGES[package]:
        return []
    return [
        Hit(SETTINGS_READ, name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in _imported_readers(node)
    ]

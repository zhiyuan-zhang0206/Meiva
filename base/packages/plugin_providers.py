"""Build an immutable catalog from enabled provider declarations.

Composition roots own the returned value and supply it to their consumers.
A fresh builder contains each attempt; load and contract errors propagate with
no partial catalog installed. Disabled plugins and plugins with no provider
face contribute nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from base.lm.catalog import CatalogBuilder, ModelCatalog
from base.lm.pricing.archive import load_archive
from base.packages.plugins.extensions import PluginContributions


# meiva:link base/lm/docs/provider-plugins.meiva.okf.md
def _load_one(
    builder: CatalogBuilder, name: str, provider_py: Path, *, is_builtin: bool, home: Path
) -> None:
    from base.packages.plugins.gate import check_manifest

    pkg = "meiva_builtins.plugins" if is_builtin else "plugins"
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}.provider", provider_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"provider plugin {name!r}: spec_from_file_location returned None "
            f"for existing {provider_py}"
        )
    module = importlib.util.module_from_spec(spec)
    # Register into sys.modules BEFORE exec_module — the same idiom as the
    # plugin.py loader: pydantic models defined inside the module need their
    # module globals reachable for get_type_hints / ForwardRef resolution.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        contributions = _declaration(module, name=name, plugin_dir=provider_py.parent, home=home)
        check_manifest(name, provider_py.parent, contributions, ("providers", "decisionProviders"))
        for contribution in contributions.providers:
            builder.install(name, contribution)
        for decision in contributions.decision_providers:
            builder.install_decision(name, decision)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise


# meiva:link base/lm/docs/provider-plugins.meiva.okf.md
def _declaration(
    module: ModuleType, *, name: str, plugin_dir: Path, home: Path
) -> PluginContributions:
    from base.packages.plugins.config_face import declared_config_class, disk_image_path
    from base.packages.plugins.config_view import read_plugin_config
    from base.packages.plugins.data_registry import declaration_of

    if not hasattr(module, "configure"):
        return declaration_of(module)
    if hasattr(module, "contribute"):
        raise ValueError(f"provider {name!r} declares both configure() and contribute()")
    configure = module.configure
    if not callable(configure):
        raise TypeError(f"provider {name!r} configure must be callable")
    cls = declared_config_class(name, plugin_dir)
    if cls is None:
        raise ValueError(f"configured provider {name!r} requires a default_config.py schema")
    config = read_plugin_config(name, cls, disk_image_path(name, home=home))
    contributions = configure(config)
    if not isinstance(contributions, PluginContributions):
        raise TypeError(f"provider {name!r} configure() did not return PluginContributions")
    return contributions


def build_model_catalog(*, home: Path) -> ModelCatalog:
    """Import every enabled plugin's ``provider.py`` into a fresh builder and build it."""
    from base import paths
    from base.packages.plugins import enable_config

    builder = CatalogBuilder(load_archive())
    discovered = enable_config.discover_plugins(home=home)
    known = set(discovered)
    config = enable_config.load_for_runtime(known, home=home)
    repo_dir = str(paths.repo_plugins_dir())
    for name in sorted(config.plugins):
        if not config.plugins[name].enabled:
            continue
        plugin_dir = discovered.get(name)
        if plugin_dir is None:
            continue
        provider_py = plugin_dir / "provider.py"
        if not provider_py.exists():
            continue
        is_builtin = repo_dir in str(plugin_dir.resolve())
        _load_one(builder, name, provider_py, is_builtin=is_builtin, home=home)
    if not builder.has_bindings:
        raise RuntimeError(
            "no provider plugins enabled — enable at least one provider plugin "
            "(the repo ships the lm_* default set; check the plugin enable config)"
        )
    return builder.build()

# meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import BaseModel, ValidationError

from base import paths
from base.packages.plugins import load_report
from base.packages.skills import names as skill_naming


class PluginEntry(BaseModel):
    """Enable state of a single plugin."""

    enabled: bool


class PluginsConfig(BaseModel):
    """Plugin config — flat dict; all plugins treated equally."""

    plugins: dict[str, PluginEntry] = {}


# Error hierarchy


class PluginsConfigError(Exception):
    """Root of plugins.json read or validation failures."""


class SchemaInvalid(PluginsConfigError):  # noqa: N818
    """JSON is valid but does not match the PluginsConfig schema (Pydantic validation failed)."""


class DanglingPlugin(PluginsConfigError):  # noqa: N818
    """A plugin name referenced by config does not exist on the filesystem.

    ``names`` carries the offending names (the message renders them too), so
    a fail-soft caller can report each one without parsing the message.
    """

    def __init__(self, message: str, *, names: set[str] | None = None) -> None:
        super().__init__(message)
        self.names: set[str] = names or set()


class DuplicatePlugin(PluginsConfigError):  # noqa: N818
    """Same plugin name exists in both builtin and external directories."""


def discover_plugins(*, home: Path) -> dict[str, Path]:
    """Scan builtin + external plugin directories; return {name: plugin_dir}.

    Builtin is scanned first; external plugin matching a builtin name
    -> DuplicatePlugin.

    Dot-prefixed directories (`.name.staging`, `.name.backup-<pid>` — the
    atomic-install residue a hard kill can leave behind, 2026-08-28
    meiva_ledger defense line) are skipped: they are transient by name and must
    never surface as ghost plugins.
    """
    from base.host.runtime_interpreter import external_plugin_read_root

    return discover_plugin_dirs(
        repo_dir=paths.repo_plugins_dir(), user_dir=external_plugin_read_root(home=home)
    )


def discover_plugin_dirs(*, repo_dir: Path, user_dir: Path) -> dict[str, Path]:
    # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
    discovered: dict[str, Path] = {}
    if repo_dir.exists():
        for p in sorted(repo_dir.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_dir() and (p / "plugin.py").exists():
                discovered[p.name] = p

    if user_dir.exists():
        for p in sorted(user_dir.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_dir() and (p / "plugin.py").exists():
                if p.name in discovered:
                    raise DuplicatePlugin(
                        f"plugin '{p.name}' exists in both builtin ({discovered[p.name]}) "
                        f"and external ({p}); same name not allowed. Delete or rename one."
                    )
                discovered[p.name] = p

    return discovered


def parse_description(source_path: Path) -> str:
    """AST-parse the module file; extract `__description__ = "..."` or
    `__description__: str = "..."` value; fall back to first line of
    the module docstring.

    Raises:
        OSError: file unreadable.
        SyntaxError: module file Python syntax invalid.
    """
    import ast

    tree = ast.parse(source_path.read_text())

    fallback = ""
    docstring = ast.get_docstring(tree)
    if docstring:
        fallback = docstring.strip().split("\n", 1)[0].strip()

    def _is_description_string(value: ast.expr) -> str | None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__description__":
                    s = _is_description_string(node.value)
                    if s is not None:
                        return s
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__description__"
            and node.value is not None
        ):
            s = _is_description_string(node.value)
            if s is not None:
                return s
    return fallback


# Core API


def _validate_schema(data: dict[str, Any]) -> PluginsConfig:
    try:
        return PluginsConfig.model_validate(data)
    except ValidationError as e:
        raise SchemaInvalid(f"plugins.json schema invalid: {e}") from e


def _default_config(known_plugins: set[str]) -> PluginsConfig:
    return PluginsConfig(
        plugins={name: PluginEntry(enabled=True) for name in sorted(known_plugins)}
    )


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


@dataclass(frozen=True)
class PluginEnableStore:
    # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md

    path: Path

    def read(self) -> dict[str, Any] | None:
        # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
        p = self.path
        if not p.exists():
            return None
        raw = p.read_text()
        data = json.loads(raw if raw.strip() else "{}")
        if not _is_str_dict(data):
            raise PluginsConfigError(f"{p}: expected a JSON object, got {type(data).__name__}")
        return data

    def write(self, raw: dict[str, Any]) -> None:
        # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
        self.path.write_text(json.dumps(raw, indent=2))

    def load(self, known_plugins: set[str], *, allow_dangling: bool = False) -> PluginsConfig:
        # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
        raw = self.read() or {}

        if not raw or not raw.get("plugins"):
            return _default_config(known_plugins)

        cfg = _validate_schema(raw)

        # meiva:note
        # Fold each config key onto the plugin's real directory spelling. A plugin
        # directory has to stay a Python package (`meiva_builtins.plugins.<name>` is
        # imported by path), so underscore is the on-disk identity while dash is the
        # name everywhere a human writes it — a hand-edited `plugins_config.json`
        # saying `meiva-code` addresses the `meiva_code` plugin.
        cfg.plugins = {
            (skill_naming.find(name, known_plugins) or name): entry
            for name, entry in cfg.plugins.items()
        }

        dangling = set(cfg.plugins) - known_plugins
        if dangling:
            if not allow_dangling:
                raise DanglingPlugin(
                    f"plugins config references non-existent plugins: {sorted(dangling)} "
                    f"(known: {sorted(known_plugins) or '<none>'})",
                    names=dangling,
                )
            cfg.plugins = {
                name: entry for name, entry in cfg.plugins.items() if name not in dangling
            }

        new_plugins = known_plugins - set(cfg.plugins)
        for name in sorted(new_plugins):
            cfg.plugins[name] = PluginEntry(enabled=True)

        return cfg

    def set_enabled(self, name: str, *, enabled: bool, known: set[str]) -> PluginsConfig:
        # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
        resolved = skill_naming.find(name, known)
        if resolved is None:
            raise DanglingPlugin(
                f"plugin {name!r} is not present on this machine (known: {sorted(known) or '<none>'})"
            )
        name = resolved
        raw = self.read() or {}
        existing = _validate_schema(raw).plugins if raw.get("plugins") else {}
        # meiva:note
        # Sorted so the written plugins.json key order (and thus the on-disk
        # iteration order that drives hook registration order) is deterministic
        # rather than set-hash-dependent.
        plugins = {n: existing.get(n, PluginEntry(enabled=True)) for n in sorted(known)}
        plugins[name] = PluginEntry(enabled=enabled)
        cfg = PluginsConfig(plugins=plugins)
        self.write(cfg.model_dump())
        return cfg

    def load_for_runtime(
        self, known_plugins: set[str], *, report: load_report.Reporter
    ) -> PluginsConfig:
        # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
        try:
            return self.load(known_plugins)
        except DanglingPlugin as exc:
            report_dangling(exc, report)
            return self.load(known_plugins, allow_dangling=True)


def local_config_path(*, home: Path) -> Path:
    """Per-machine plugin-config file path: `~/.meiva/plugins_config.json`."""
    return home / "plugins_config.json"


def write_local(raw: dict[str, Any], *, home: Path) -> None:
    # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
    PluginEnableStore(local_config_path(home=home)).write(raw)


def load(known_plugins: set[str], *, allow_dangling: bool = False, home: Path) -> PluginsConfig:
    # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
    return PluginEnableStore(local_config_path(home=home)).load(
        known_plugins, allow_dangling=allow_dangling
    )


def report_dangling(exc: DanglingPlugin, report: load_report.Reporter | None = None) -> None:
    """Report dangling config entries through the one canonical fail-soft reporter.

    Both dangling-handling load sites — the runtime wrapper below and the
    agent-boot loader (`agent/extensions/__init__.py`) — drop the entries (treated as
    disabled) and report each name through `base.packages.plugins.load_report`, so a
    plugin that is enabled in the machine config but absent from disk is as
    visible as every other contained plugin failure: loguru ERROR plus the
    `plugin_load_failed` telemetry event. The 2026-09-11 macmini incident
    (`codex_usage` / `deepseek_balance` enabled with their directories gone)
    ran for days on a plain WARNING that only a log grep ever found. Each name
    reports on each failed load; the caller keeps the
    fail-soft contract — dropping the entry must never block a start.
    """
    for name in sorted(set(exc.names)):
        load_report.reporter(report)(name, exc)


def load_for_runtime(known_plugins: set[str], *, home: Path) -> PluginsConfig:
    # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
    return PluginEnableStore(local_config_path(home=home)).load_for_runtime(
        known_plugins, report=load_report.reporter(None)
    )


def installed_plugin_dirs(*, home: Path) -> dict[str, Path]:
    """{name: plugin_dir} for every plugin PRESENT on this machine (builtin +
    external), regardless of enable-state.

    A thin public alias for `discover_plugins()`. The service roster
    (`services.supervision.roster.plugin_services.plugin_services`) uses this — presence, NOT the agent-facing
    enable-state (`meiva plugins enable/disable`) — to fold plugin-declared
    ServiceSpecs into `build_services()`. The roster is a machine/cluster concern
    and must not depend on the agent-plugin-registration plane; a plugin gates its
    own service via an explicit settings field in `ServiceSpec.gate` (e.g.
    task-maintenance's Fleet `task_maintenance_enabled`).
    """
    return discover_plugins(home=home)


def set_local_enabled(name: str, *, enabled: bool, home: Path) -> PluginsConfig:
    # meiva:link base/packages/plugins/docs/plugin-config/enable-state.meiva.okf.md
    return PluginEnableStore(local_config_path(home=home)).set_enabled(
        name, enabled=enabled, known=set(discover_plugins(home=home))
    )

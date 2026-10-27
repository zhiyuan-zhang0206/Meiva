from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any, TypedDict, TypeGuard

from base.host.runtime_interpreter import external_plugin_read_root
from base.host.system.hostport import Host
from base.host.system.probes import display_available
from base.paths import mcps_dir, repo_root


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _is_server_map(value: object) -> TypeGuard[dict[str, dict[str, Any]]]:
    return isinstance(value, dict)


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
class MCPError(Exception):
    pass


# meiva:note
# Preserves the public SDK exception name (`MCPServerNotFound`), matching stdlib
# `FileNotFoundError` rather than an `*Error` suffix.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
class MCPServerNotFound(MCPError):  # noqa: N818
    pass


class MCPConnectError(MCPError):
    pass


# meiva:note
# Preserves the public SDK exception name (`MCPToolNotFound`), matching
# `MCPServerNotFound` and stdlib `FileNotFoundError`.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
class MCPToolNotFound(MCPError):  # noqa: N818
    pass


class MCPCallError(MCPError):
    pass


class ToolInfo(TypedDict):
    name: str
    description: str
    input_schema: dict[str, Any]


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def validate_requirements(spec: dict[str, Any]) -> dict[str, bool]:
    requires: object = spec.get("requires")
    if requires is None:
        return {}
    if not _is_object_dict(requires):
        raise MCPError("server 'requires' must be an object or null")
    validated: dict[str, bool] = {}
    for key, want in requires.items():
        if not isinstance(key, str) or key != "display":
            raise MCPError(f"unknown requires key {key!r} in MCP server config")
        if not isinstance(want, bool):
            raise MCPError(f"requires {key!r} must be a boolean")
        validated[key] = want
    return validated


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def assert_requirements(spec: dict[str, Any], *, host: Host) -> None:
    requires = validate_requirements(spec)
    for key, want in requires.items():
        if key == "display" and want and not display_available(host=host):
            raise MCPError(
                "MCP server requires a display, but this host has none (headless server)"
            )


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def server_capability(spec: dict[str, Any], *, host: Host) -> tuple[bool, str | None]:
    requires = validate_requirements(spec)
    if requires.get("display") and not display_available(host=host):
        return (False, "requires a display, but this host has none")
    return (True, None)


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def server_url(spec: dict[str, Any]) -> str | None:
    url = spec.get("url")
    if url is None:
        return None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise MCPError(f"server 'url' must be an http(s) endpoint, got {url!r} (expected a string)")
    if spec.get("command") is not None:
        raise MCPError("server declares both 'url' and 'command' — pick one transport")
    headers: object = spec.get("headers")
    if headers is not None and not (
        _is_object_dict(headers)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
    ):
        raise MCPError(f"server 'headers' must be a dict of str -> str, got {headers!r}")
    oauth = spec.get("oauth")
    if oauth is not None and not isinstance(oauth, bool):
        raise MCPError(f"server 'oauth' must be a boolean, got {oauth!r}")
    if oauth and headers:
        raise MCPError("server declares both 'oauth' and 'headers' — pick one auth mode")
    return url


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def machine_config_path(*, home: Path) -> Path:
    return home / "mcp.json"


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def read_servers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise MCPError(f"Failed to read {path}: {type(e).__name__}: {e}") from e
    if not _is_str_dict(data):
        raise MCPError(f"`{path}` is not a JSON object (got {type(data).__name__})")
    section: object = data.get("mcpServers")
    if section is None:
        return {}
    if not _is_server_map(section):
        raise MCPError(f"`{path}` mcpServers field is not a dict (got {type(section).__name__})")
    return section


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def builtin_mcp_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[2] / "meiva_builtins" / "mcps"
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for entry in sorted(root.iterdir()):
        mcp_json = entry / ".mcp.json"
        if mcp_json.is_file():
            paths.append(mcp_json)
    return paths


def _installed_mcp_paths(*, home: Path) -> list[Path]:
    from base.packages.extensions.install_registry import installed_mcp_names

    root = mcps_dir(home)
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for name in sorted(installed_mcp_names(home=home)):
        mcp_json = root / name / ".mcp.json"
        if mcp_json.is_file():
            paths.append(mcp_json)
    return paths


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def installed_mcp_dir(name: str, *, home: Path) -> Path | None:
    from base.packages.extensions.install_registry import installed_mcp_names

    if name not in installed_mcp_names(home=home):
        return None
    if name in read_servers(machine_config_path(home=home)):
        return None
    d = mcps_dir(home) / name
    return d if (d / ".mcp.json").is_file() else None


# meiva:note
# Resolves the working directory for spawning `name`'s server by layer: machine and
# plugin entries return `None` (third-party command line), installed MCPs return
# `$MEIVA_HOME/mcps/<name>/` (package venv), and built-in MCPs return `repo_root()`.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def server_cwd(name: str, *, home: Path) -> Path | None:
    if name in read_servers(machine_config_path(home=home)):
        return None
    installed = installed_mcp_dir(name, home=home)
    if installed is not None:
        return installed
    for path in plugin_config_paths(home=home):
        if name in read_servers(path):
            return None
    for path in builtin_mcp_paths():
        if name in read_servers(path):
            return repo_root()
    return None


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def plugin_config_paths(*, home: Path) -> list[Path]:
    paths: list[Path] = []
    repo_plugins = Path(__file__).resolve().parents[2] / "meiva_builtins" / "plugins"
    for root in (repo_plugins, external_plugin_read_root(home=home)):
        if not root.is_dir():
            continue
        for plugin_dir in sorted(root.iterdir()):
            if plugin_dir.name.startswith("."):
                continue
            mcp_json = plugin_dir / ".mcp.json"
            if mcp_json.is_file():
                paths.append(mcp_json)
    return paths


# meiva:note
# Merges MCP server declarations in layer order (`meiva_builtins/mcps/` < plugin
# `.mcp.json` < installed `$MEIVA_HOME/mcps/` < `$MEIVA_HOME/mcp.json`) and filters out
# servers disabled in `mcp_enabled.json` unless `include_disabled=True`.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def load_mcp_config(*, include_disabled: bool = False, home: Path) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in builtin_mcp_paths():
        merged.update(read_servers(path))
    for path in plugin_config_paths(home=home):
        merged.update(read_servers(path))
    for path in _installed_mcp_paths(home=home):
        merged.update(read_servers(path))
    merged.update(read_servers(machine_config_path(home=home)))
    if include_disabled:
        return merged
    from base.packages.plugins.mcp_enabled import read_enabled

    enabled = read_enabled(home=home)
    return {k: v for k, v in merged.items() if enabled.get(k, True)}


# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
def is_transport_error(exc: BaseException) -> bool:
    from anyio import BrokenResourceError, ClosedResourceError
    from mcp import MCPError as MCPProtocolError
    from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

    if isinstance(exc, (BrokenResourceError, ClosedResourceError, TimeoutError)):
        return True
    if isinstance(exc, OSError):
        return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionRefusedError)) or (
            exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ECONNREFUSED)
        )
    if isinstance(exc, MCPProtocolError):
        if exc.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT):
            return True
        if exc.__cause__ is not None:
            return is_transport_error(exc.__cause__)
    return False

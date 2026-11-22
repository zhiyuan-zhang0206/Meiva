from __future__ import annotations

import ipaddress
import re
import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from base.host.boundary.json_values import is_object_dict

RemoteHostOs = Literal["windows", "macos", "linux"]
RemoteHostProtocol = Literal["win_bridge_mcp", "computer_http"]
TargetKind = Literal["local", "ssh", "meiva"]

_VALID_OS: frozenset[str] = frozenset({"windows", "macos", "linux"})
_VALID_PROTOCOLS: frozenset[str] = frozenset({"win_bridge_mcp", "computer_http"})
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_HOST_KEYS: frozenset[str] = frozenset(
    {
        "os",
        "ssh_host",
        "helper_port",
        "local_port",
        "token_file",
        "notes",
        "capabilities",
        "protocol",
    }
)
DEFAULT_WIN_HELPER_PORT = 18792


class RemoteHostConfigError(ValueError):
    pass


class TargetResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteHostConfig:
    alias: str
    os: RemoteHostOs
    ssh_host: str
    helper_port: int = DEFAULT_WIN_HELPER_PORT
    local_port: int | None = None
    token_file: str | None = None
    notes: str = ""
    capabilities: str = ""
    protocol: RemoteHostProtocol = "win_bridge_mcp"

    @property
    def target_uri(self) -> str:
        return f"ssh://{self.alias}"

    @property
    def summary_notes(self) -> str:
        parts = [p.strip() for p in (self.notes, self.capabilities) if p.strip()]
        return " | ".join(parts)

    def __repr__(self) -> str:
        return (
            f"RemoteHostConfig(alias={self.alias!r}, os={self.os!r}, "
            f"ssh_host={self.ssh_host!r}, helper_port={self.helper_port!r}, "
            f"local_port={self.local_port!r}, protocol={self.protocol!r})"
        )


@dataclass(frozen=True)
class ResolvedTarget:
    kind: TargetKind
    name: str
    canonical_uri: str


def is_ip_literal(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if candidate.startswith("["):
        end = candidate.find("]")
        if end != -1:
            candidate = candidate[1:end]
    elif candidate.count(":") == 1:
        host_part, port_part = candidate.rsplit(":", 1)
        if port_part.isdigit():
            candidate = host_part
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def _validate_port(value: object, field_name: str, alias: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
        raise RemoteHostConfigError(
            f"remote_hosts.toml: host {alias!r} field {field_name!r} must be an integer 1..65535"
        )
    return value


def _validate_optional_str(value: object, field_name: str, alias: str) -> str:
    if not isinstance(value, str):
        raise RemoteHostConfigError(
            f"remote_hosts.toml: host {alias!r} field {field_name!r} must be a string"
        )
    return value.strip()


def _parse_os(raw: dict[str, object], alias: str) -> RemoteHostOs:
    raw_os = raw.get("os", "windows")
    if not isinstance(raw_os, str) or raw_os.strip().lower() not in _VALID_OS:
        raise RemoteHostConfigError(
            f"remote_hosts.toml: host {alias!r} 'os' must be one of windows, macos, linux"
        )
    os_name = raw_os.strip().lower()
    if os_name == "windows":
        return "windows"
    return "macos" if os_name == "macos" else "linux"


def _parse_protocol(
    raw: dict[str, object], alias: str, typed_os: RemoteHostOs
) -> RemoteHostProtocol:
    if "protocol" not in raw:
        return "win_bridge_mcp" if typed_os == "windows" else "computer_http"
    raw_proto = _validate_optional_str(raw["protocol"], "protocol", alias).lower()
    if raw_proto not in _VALID_PROTOCOLS:
        raise RemoteHostConfigError(
            f"remote_hosts.toml: host {alias!r} 'protocol' must be one of "
            f"{', '.join(sorted(_VALID_PROTOCOLS))}"
        )
    return "win_bridge_mcp" if raw_proto == "win_bridge_mcp" else "computer_http"


def _parse_ssh_host(raw: dict[str, object], alias: str) -> str:
    ssh_host = (
        _validate_optional_str(raw["ssh_host"], "ssh_host", alias) if "ssh_host" in raw else alias
    )
    if not ssh_host or not _SSH_HOST_RE.fullmatch(ssh_host):
        raise RemoteHostConfigError(
            f"remote_hosts.toml: host {alias!r} 'ssh_host' must be a safe SSH config alias "
            "(alphanumeric/dot/underscore/hyphen, cannot start with '-')"
        )
    return ssh_host


def _optional_port(raw: dict[str, object], field_name: str, alias: str) -> int | None:
    if field_name not in raw or raw[field_name] is None:
        return None
    return _validate_port(raw[field_name], field_name, alias)


def _optional_text(raw: dict[str, object], field_name: str, alias: str) -> str:
    return _validate_optional_str(raw[field_name], field_name, alias) if field_name in raw else ""


def _parse_host_entry(alias: str, raw: object) -> RemoteHostConfig:
    if not _ALIAS_RE.fullmatch(alias) or is_ip_literal(alias):
        raise RemoteHostConfigError(
            f"remote_hosts.toml: invalid host alias {alias!r} "
            "(must be an identifier/hostname alias, never a bare IP address)"
        )
    if not is_object_dict(raw):
        raise RemoteHostConfigError(f"remote_hosts.toml: [hosts.{alias}] must be a table")
    unknown = set(raw.keys()) - _ALLOWED_HOST_KEYS
    if unknown:
        raise RemoteHostConfigError(
            f"remote_hosts.toml: host {alias!r} has unknown key(s): {', '.join(sorted(unknown))}"
        )
    typed_os = _parse_os(raw, alias)
    helper_port = _optional_port(raw, "helper_port", alias) or DEFAULT_WIN_HELPER_PORT
    raw_token = raw.get("token_file")
    token_file = (
        _validate_optional_str(raw_token, "token_file", alias) or None
        if raw_token is not None
        else None
    )
    return RemoteHostConfig(
        alias=alias,
        os=typed_os,
        ssh_host=_parse_ssh_host(raw, alias),
        helper_port=helper_port,
        local_port=_optional_port(raw, "local_port", alias),
        token_file=token_file,
        notes=_optional_text(raw, "notes", alias),
        capabilities=_optional_text(raw, "capabilities", alias),
        protocol=_parse_protocol(raw, alias, typed_os),
    )


def parse_remote_hosts_toml(raw_toml: str) -> dict[str, RemoteHostConfig]:
    try:
        parsed: object = tomllib.loads(raw_toml)
    except tomllib.TOMLDecodeError as exc:
        raise RemoteHostConfigError(f"invalid remote_hosts.toml: {exc}") from exc
    if not is_object_dict(parsed):
        raise RemoteHostConfigError("remote_hosts.toml root must be a TOML table")
    unknown_top = set(parsed.keys()) - {"hosts"}
    if unknown_top:
        raise RemoteHostConfigError(
            f"remote_hosts.toml has unknown top-level section(s): {', '.join(sorted(unknown_top))}"
        )
    hosts_section = parsed.get("hosts")
    if hosts_section is None:
        return {}
    if not is_object_dict(hosts_section):
        raise RemoteHostConfigError("remote_hosts.toml: [hosts] must be a table")
    return {alias: _parse_host_entry(alias, entry) for alias, entry in hosts_section.items()}


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _validate_target_name(raw_name: str, *, raw_host: str) -> str:
    if (
        not raw_name
        or ".." in raw_name
        or any(
            ch in ("/", "\\", ":", "@", "?", "#")
            or ch.isspace()
            or ord(ch) < 0x20
            or ord(ch) == 0x7F
            for ch in raw_name
        )
    ):
        raise TargetResolutionError(
            f"Invalid computer_use host {raw_host!r}: bare URLs, ports, paths, and raw IPs are "
            "forbidden. Run `meiva computer list` to view registered targets."
        )
    if is_ip_literal(raw_name):
        raise TargetResolutionError(
            f"Invalid computer_use host {raw_host!r}: raw IP addresses are forbidden (SSRF guard). "
            "Register the host in remote_hosts.toml and run `meiva computer list`."
        )
    return raw_name


def _cluster_or_local(machine: str, local_machine: str | None) -> ResolvedTarget:
    if local_machine is not None and machine == local_machine:
        return ResolvedTarget(kind="local", name="local", canonical_uri="local")
    return ResolvedTarget(kind="meiva", name=machine, canonical_uri=f"meiva://{machine}")


def _resolve_scheme_target(
    cleaned: str,
    *,
    raw_host: str,
    ssh_set: set[str],
    cluster_set: set[str],
    local_machine: str | None,
) -> ResolvedTarget:
    scheme, _, remainder = cleaned.partition("://")
    scheme_lower = scheme.lower()
    if scheme_lower == "ssh":
        alias = _validate_target_name(remainder, raw_host=raw_host)
        if alias not in ssh_set:
            raise TargetResolutionError(
                f"Unknown SSH desktop host 'ssh://{alias}'. "
                "Run `meiva computer list` to view registered targets."
            )
        return ResolvedTarget(kind="ssh", name=alias, canonical_uri=f"ssh://{alias}")
    if scheme_lower == "meiva":
        machine = _validate_target_name(remainder, raw_host=raw_host)
        if machine not in cluster_set:
            raise TargetResolutionError(
                f"Unknown cluster desktop host 'meiva://{machine}'. "
                "Run `meiva computer list` to view registered targets."
            )
        return _cluster_or_local(machine, local_machine)
    raise TargetResolutionError(
        f"Unsupported computer_use host scheme in {raw_host!r}: only 'local', 'meiva://<machine>', "
        "and 'ssh://<alias>' are allowed. Run `meiva computer list` to view registered targets."
    )


def resolve_target(
    host: str | None,
    *,
    ssh_aliases: Collection[str],
    cluster_machines: Collection[str],
    local_machine: str | None = None,
) -> ResolvedTarget:
    if host is None:
        return ResolvedTarget(kind="local", name="local", canonical_uri="local")
    if _has_control_chars(host):
        raise TargetResolutionError(
            f"Invalid computer_use host {host!r}: control characters are forbidden. "
            "Run `meiva computer list` to view registered targets."
        )
    cleaned = host.strip()
    if not cleaned or cleaned.lower() in {"local", "local://"}:
        return ResolvedTarget(kind="local", name="local", canonical_uri="local")
    ssh_set, cluster_set = set(ssh_aliases), set(cluster_machines)
    if "://" in cleaned:
        return _resolve_scheme_target(
            cleaned,
            raw_host=host,
            ssh_set=ssh_set,
            cluster_set=cluster_set,
            local_machine=local_machine,
        )
    short = _validate_target_name(cleaned, raw_host=host)
    in_ssh, in_cluster = short in ssh_set, short in cluster_set
    if in_ssh and in_cluster:
        raise TargetResolutionError(
            f"Ambiguous computer_use host {short!r}: matches both 'ssh://{short}' and "
            f"'meiva://{short}'. Specify the explicit URI scheme."
        )
    if in_ssh:
        return ResolvedTarget(kind="ssh", name=short, canonical_uri=f"ssh://{short}")
    if in_cluster:
        return _cluster_or_local(short, local_machine)
    raise TargetResolutionError(
        f"Unknown computer_use host {host!r}: only registered targets are allowed. "
        "Run `meiva computer list` to view registered targets."
    )

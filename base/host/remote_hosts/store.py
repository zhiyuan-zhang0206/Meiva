from __future__ import annotations

from pathlib import Path

from base.paths import remote_hosts_path

from .plan import RemoteHostConfig, RemoteHostConfigError, parse_remote_hosts_toml


def load_remote_hosts(*, home: Path) -> dict[str, RemoteHostConfig]:
    path = remote_hosts_path(home)
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RemoteHostConfigError(f"Failed to read {path}: {exc}") from exc
    return parse_remote_hosts_toml(raw)


def has_remote_hosts(*, home: Path) -> bool:
    path = remote_hosts_path(home)
    if not path.is_file():
        return False
    try:
        return bool(parse_remote_hosts_toml(path.read_text(encoding="utf-8")))
    except (OSError, RemoteHostConfigError):
        return False


def _resolve_secret_path(raw_path: str, *, home: Path, user_home: Path) -> Path:
    if raw_path == "~":
        return user_home
    if raw_path.startswith("~/"):
        return user_home / raw_path[2:]
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return home / candidate


def read_bearer_token(raw_path: str, *, home: Path, user_home: Path) -> str:
    path = _resolve_secret_path(raw_path, home=home, user_home=user_home)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RemoteHostConfigError(
            f"Could not read Bearer token file ({type(exc).__name__})"
        ) from exc
    if not raw:
        raise RemoteHostConfigError("Bearer token file is empty")
    return raw

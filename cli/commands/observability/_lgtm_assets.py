"""Pinned native LGTM assets and executable arguments for supported hosts."""

from __future__ import annotations

from pathlib import Path
from typing import TypeGuard

import yaml

from base.telemetry.lgtm_local import NATIVE_SERVICES

NATIVE_CONSTANTS = NATIVE_SERVICES


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def load_versions(repo: Path, tag: str) -> dict[str, dict[str, str]]:
    """Read the same pinned versions with platform-specific verified archives."""
    raw = yaml.safe_load((repo / "deploy/lgtm/native/versions.yml").read_text(encoding="utf-8"))
    if not _is_object_dict(raw):
        raise TypeError("deploy/lgtm/native/versions.yml must contain a mapping")
    entries = raw
    versions: dict[str, dict[str, str]] = {}
    asset_tag = tag.replace("_", "-")
    for name, constants in NATIVE_CONSTANTS.items():
        service = entries[name]
        if not _is_object_dict(service):
            raise TypeError(f"native LGTM version entry for {name} must be a mapping")
        values = service
        version, assets = values["version"], values["assets"]
        if not isinstance(version, str) or not _is_object_dict(assets):
            raise TypeError(f"native LGTM version entry for {name} is invalid")
        asset = assets[asset_tag]
        if not _is_object_dict(asset):
            raise TypeError(f"native LGTM {asset_tag} asset for {name} must be a mapping")
        data = asset
        url, sha256 = data["url"], data["sha256"]
        if not isinstance(url, str) or not isinstance(sha256, str):
            raise TypeError(f"native LGTM {asset_tag} asset for {name} is invalid")
        member = constants.archive_member
        versions[name] = {
            "version": version,
            "url": url,
            "sha256": sha256,
            "member": member.replace("darwin-arm64", asset_tag) if member else "",
        }
    return versions

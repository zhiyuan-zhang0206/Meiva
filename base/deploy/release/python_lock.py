"""Dependency-free canonical lock validation shared by bootstrap and CI."""

from __future__ import annotations

import tomllib

_INDEX = "https://pypi.org/simple"
_ARTIFACT_PREFIX = "https://files.pythonhosted.org/packages/"


def violations(lock_text: str) -> list[str]:
    """Report registry packages whose index or artifact transport is host-specific."""
    lock = tomllib.loads(lock_text)
    errors: list[str] = []
    for package in lock["package"]:
        source = package["source"]
        if "registry" not in source:
            continue
        name = package["name"]
        if source["registry"] != _INDEX:
            errors.append(f"{name}: registry must be {_INDEX}")
        artifacts = list(package.get("wheels", []))
        if "sdist" in package:
            artifacts.append(package["sdist"])
        if any(not item["url"].startswith(_ARTIFACT_PREFIX) for item in artifacts):
            errors.append(f"{name}: distribution URLs must use {_ARTIFACT_PREFIX}")
    return errors

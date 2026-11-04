"""Dependency-free canonical lock validation shared by bootstrap and CI."""

from __future__ import annotations

import tomllib

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_mapping

_INDEX = "https://pypi.org/simple"
_ARTIFACT_PREFIX = "https://files.pythonhosted.org/packages/"


# meiva:note
# The `url` of one wheel or sdist table in a lock package; a lock whose entry is not a table with
# a string `url` is malformed, so this raises instead of guessing.
def _artifact_url(item: object) -> str:
    if not is_object_dict(item):
        raise TypeError(f"lock distribution entry is {type(item).__name__}, not a table")
    url = item["url"]
    if not isinstance(url, str):
        raise TypeError(f"lock distribution url is {type(url).__name__}, not a string")
    return url


def violations(lock_text: str) -> list[str]:
    """Report registry packages whose index or artifact transport is host-specific."""
    lock = object_mapping(tomllib.loads(lock_text))
    errors: list[str] = []
    packages = lock["package"]
    if not is_object_list(packages):
        raise TypeError(f"lock `package` is {type(packages).__name__}, not an array")
    for package in packages:
        if not is_object_dict(package):
            raise TypeError(f"lock package entry is {type(package).__name__}, not a table")
        source = package["source"]
        if not is_object_dict(source):
            raise TypeError(f"lock package source is {type(source).__name__}, not a table")
        if "registry" not in source:
            continue
        name = package["name"]
        if source["registry"] != _INDEX:
            errors.append(f"{name}: registry must be {_INDEX}")
        wheels = package.get("wheels", [])
        if not is_object_list(wheels):
            raise TypeError(f"lock package wheels is {type(wheels).__name__}, not an array")
        artifacts = list(wheels)
        if "sdist" in package:
            artifacts.append(package["sdist"])
        if any(not _artifact_url(item).startswith(_ARTIFACT_PREFIX) for item in artifacts):
            errors.append(f"{name}: distribution URLs must use {_ARTIFACT_PREFIX}")
    return errors

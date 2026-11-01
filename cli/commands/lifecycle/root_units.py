# meiva:note
# What the requested tree says about its units: the launch manifest is a mapping of unit rows, and
# the digest of each row is what a running root is compared against. Reading it lives here so the
# driver that launches, reconciles and stops the tree is not also the module that parses it.

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeGuard

__all__ = ["unit_digests"]


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The requested manifest digest of every unit, by unit id.
def unit_digests(manifest: Mapping[str, object]) -> dict[str, str]:
    from services.supervision.meiva_root.manifest import UnitManifest

    rows = manifest["units"]
    if not _is_object_list(rows):
        raise TypeError("the launch manifest carries no unit list")
    desired = (
        UnitManifest.from_mapping(row, origin="requested tree")
        for row in rows
        if _is_object_dict(row)
    )
    return {unit.id: unit.digest() for unit in desired}

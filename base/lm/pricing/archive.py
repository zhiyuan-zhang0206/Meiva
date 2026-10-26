# meiva:note
# The pricing archive as shipped with the package: the one read of its bytes, parsed by
# `parse_archive` in the package door.

from __future__ import annotations

import pkgutil

from base.lm.pricing import ModelPrice, parse_archive

__all__ = ["load_archive"]


def load_archive() -> dict[str, ModelPrice]:
    data = pkgutil.get_data("base.lm.pricing", "pricing_catalog_archive.json")
    if data is None:
        raise FileNotFoundError("pricing_catalog_archive.json is missing from base.lm.pricing")
    return parse_archive(data)

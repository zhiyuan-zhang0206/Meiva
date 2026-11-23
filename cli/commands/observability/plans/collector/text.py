# meiva:note
# How a value is quoted when it is written into the collector config.

from __future__ import annotations

__all__ = ["yaml_quote"]


# meiva:note
# A single-quoted YAML scalar: the form that needs no escaping but `''`.
def yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

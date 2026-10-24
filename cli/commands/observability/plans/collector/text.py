# meiva:note
# How a value is quoted when it is written into the collector config.

from __future__ import annotations

import json

__all__ = ["ottl_quote", "yaml_quote"]


# meiva:note
# A single-quoted YAML scalar: the form that needs no escaping but `''`.
def yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# meiva:note
# A JSON string literal, which is also an escaped OTTL string literal.
def ottl_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)

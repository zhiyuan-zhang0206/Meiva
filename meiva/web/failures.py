# meiva:note
# The error text of a failed provider request. `label` names the provider ("Brave Search",
# "Jina Reader", ...). An HTTP failure carries either the whole response body or, for a provider
# that answers errors in JSON, the message that JSON holds.

from __future__ import annotations

import json
from typing import TypeGuard

__all__ = ["connection_failed_message", "http_failed_message", "non_json_message"]

_DETAIL_MAX = 200


def _is_str_dict(val: object) -> TypeGuard[dict[str, object]]:
    return isinstance(val, dict)


def _provider_message(detail: str) -> str:
    try:
        parsed: object = json.loads(detail)
    except json.JSONDecodeError:
        return detail[:_DETAIL_MAX]
    error = parsed.get("error") if _is_str_dict(parsed) else None
    if _is_str_dict(error):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return detail[:_DETAIL_MAX]


def http_failed_message(label: str, status: int, detail: str, *, provider_json: bool) -> str:
    return f"{label} HTTP {status}: {_provider_message(detail) if provider_json else detail}"


def connection_failed_message(label: str, reason: object) -> str:
    return f"{label} connection failed: {reason}"


def non_json_message(label: str, error: json.JSONDecodeError) -> str:
    return f"{label} returned non-JSON: {error}"

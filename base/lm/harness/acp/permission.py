from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from base.lm.harness.acp.frames import JSONRPC, Json, JsonObject, RequestId


@dataclass(frozen=True, slots=True)
class PermissionOption:
    option_id: str
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    session_id: str
    tool_call: JsonObject
    options: tuple[PermissionOption, ...]

    def option(self, kind: str) -> PermissionOption | None:
        for candidate in self.options:
            if candidate.kind == kind:
                return candidate
        return None


# meiva:note
# A permission handler returns an option id or None. None is reported to the agent
# as a cancelled outcome, which the ACP contract lets it read as a refusal:
# inventing an allow would turn a missing approval surface into an unaudited grant.
PermissionHandler = Callable[[PermissionRequest], str | Awaitable[str | None] | None]


# meiva:note
# Decode a `session/request_permission` payload into a `PermissionRequest`.
def parse_permission_request(params: JsonObject) -> PermissionRequest:
    raw_options: Json = params.get("options")
    option_items = raw_options if isinstance(raw_options, list) else []
    options = tuple(
        PermissionOption(
            option_id=str(option.get("optionId", "")),
            name=str(option.get("name", "")),
            kind=str(option.get("kind", "")),
        )
        for option in option_items
        if isinstance(option, dict)
    )
    raw_tool_call: Json = params.get("toolCall")
    return PermissionRequest(
        session_id=str(params.get("sessionId", "")),
        tool_call=raw_tool_call if isinstance(raw_tool_call, dict) else {},
        options=options,
    )


# meiva:note
# Build the JSON-RPC permission outcome frame for the chosen option id.
def permission_response(
    request_id: RequestId, chosen: str | None, options: tuple[PermissionOption, ...]
) -> JsonObject:
    if chosen is None or chosen not in {option.option_id for option in options}:
        return {
            "jsonrpc": JSONRPC,
            "id": request_id,
            "result": {"outcome": {"outcome": "cancelled"}},
        }
    return {
        "jsonrpc": JSONRPC,
        "id": request_id,
        "result": {"outcome": {"outcome": "selected", "optionId": chosen}},
    }

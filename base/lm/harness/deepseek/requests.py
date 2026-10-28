# meiva:note
# What a DeepSeek session sends. The session owns the connection: it awaits each request and applies
# what comes back; the payloads are data in, payload out, and live here instead of being assembled at
# each call site.
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from base.lm.harness.acp import AcpError
from base.lm.harness.acp.permission import PermissionRequest

type JsonObject = dict[str, Any]

PROTOCOL_VERSION = 1
MODEL_CONFIG_ID = "model"
REASONING_EFFORT_CONFIG_ID = "reasoning_effort"
ALLOW_MODE = "allow"
REFUSING_MODES = frozenset({"ask", "deny"})
ALLOW_ONCE = "allow_once"
REJECT_ONCE = "reject_once"
UNNAMED_TOOL = "tool"

# meiva:note
# What this bridge says when it opens the connection: the protocol version it speaks and the client
# capabilities it claims. Every capability is a surface the runtime may then ask us to serve, so the
# empty map is the statement that we serve none of them.
INITIALIZE_PARAMS: JsonObject = {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}}


# meiva:note
# The scope both `session/new` and `session/resume` carry: where the runtime runs and which MCP
# servers it may reach. `session_id` names the session a resume continues; leaving it out is the
# `session/new` shape, where the runtime mints the id itself.
def scope_params(
    *,
    workspace: str,
    mcp_servers: Iterable[Mapping[str, object]],
    session_id: str | None = None,
) -> JsonObject:
    scope: JsonObject = {
        "cwd": workspace,
        "mcpServers": [dict(server) for server in mcp_servers],
    }
    if session_id is not None:
        scope["sessionId"] = session_id
    return scope


# meiva:note
# A `session/new` answer that names no session cannot be used: every later request addresses one, so
# a wrong id would surface as an unrelated failure much later.
def created_session_id(created: Mapping[str, object]) -> str:
    session_id = created.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise AcpError(f"session/new returned no sessionId: {created!r}")
    return session_id


# meiva:note
# The selectors the profile advertises, sent once the session exists. A configured value the
# profile does not offer is refused by the runtime, which the caller tolerates.
def config_option_requests(
    *, session_id: str | None, model: str | None, reasoning_effort: str | None
) -> tuple[JsonObject, ...]:
    if session_id is None or model is None:
        return ()
    requests = [{"sessionId": session_id, "configId": MODEL_CONFIG_ID, "value": model}]
    if reasoning_effort is not None:
        requests.append(
            {
                "sessionId": session_id,
                "configId": REASONING_EFFORT_CONFIG_ID,
                "value": reasoning_effort,
            }
        )
    return tuple(requests)


def prompt_params(*, session_id: str | None, text: str) -> JsonObject:
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}


# meiva:note
# The payload `session/cancel` and `session/close` both take: the session they address.
def session_ref(session_id: str) -> JsonObject:
    return {"sessionId": session_id}


# meiva:note
# What one permission request is answered with: the option id, and the tool named when the answer is
# a refusal.
@dataclass(frozen=True, slots=True)
class PermissionChoice:
    option_id: str | None
    denial: str | None


def _tool_label(request: PermissionRequest) -> str:
    return str(
        request.tool_call.get("title") or request.tool_call.get("toolCallId") or UNNAMED_TOOL
    )


# meiva:note
# The permission mode decides which option id answers: `allow` takes the runtime's allow-once
# option, and the modes that refuse take reject-once and name the tool, so the denial is visible to
# the model and to the turn's report. An option the request does not offer leaves the answer
# unspecified rather than guessing one. A mode outside the three `BridgeConfig` admits is refused
# here rather than answered with a denial nobody configured.
def permission_choice(*, mode: str, request: PermissionRequest) -> PermissionChoice:
    if mode == ALLOW_MODE:
        option = request.option(ALLOW_ONCE)
        return PermissionChoice(option_id=None if option is None else option.option_id, denial=None)
    if mode in REFUSING_MODES:
        denial = request.option(REJECT_ONCE)
        return PermissionChoice(
            option_id=None if denial is None else denial.option_id, denial=_tool_label(request)
        )
    raise ValueError(f"unknown permission_mode {mode!r}; known modes: allow, ask, deny")

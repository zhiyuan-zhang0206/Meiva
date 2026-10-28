from __future__ import annotations

import pytest

from base.lm.harness.acp import AcpError
from base.lm.harness.acp.permission import PermissionOption, PermissionRequest
from base.lm.harness.deepseek.requests import (
    INITIALIZE_PARAMS,
    PermissionChoice,
    config_option_requests,
    created_session_id,
    permission_choice,
    prompt_params,
    scope_params,
    session_ref,
)


def request_with(
    options: tuple[str, ...], *, tool_call: dict[str, str] | None = None
) -> PermissionRequest:
    return PermissionRequest(
        session_id="session-1",
        tool_call={"toolCallId": "call-1", "title": "write file"}
        if tool_call is None
        else tool_call,
        options=tuple(PermissionOption(option_id=name, name=name, kind=name) for name in options),
    )


def test_the_initialize_payload_claims_the_protocol_version_and_no_capabilities() -> None:
    assert INITIALIZE_PARAMS == {"protocolVersion": 1, "clientCapabilities": {}}


def test_a_new_session_scope_names_no_session_and_copies_the_servers() -> None:
    servers = ({"name": "one"},)
    params = scope_params(workspace="/work", mcp_servers=servers)
    assert params == {"cwd": "/work", "mcpServers": [{"name": "one"}]}
    assert params["mcpServers"] is not servers


def test_a_resume_scope_names_the_session_it_continues() -> None:
    assert scope_params(workspace="/work", mcp_servers=(), session_id="resumed") == {
        "cwd": "/work",
        "mcpServers": [],
        "sessionId": "resumed",
    }


def test_a_session_id_is_read_out_of_the_new_session_answer() -> None:
    assert created_session_id({"sessionId": "fresh"}) == "fresh"


@pytest.mark.parametrize("answer", [{}, {"sessionId": ""}, {"sessionId": 7}, {"sessionId": None}])
def test_an_answer_without_a_usable_session_id_is_refused(answer: dict[str, object]) -> None:
    with pytest.raises(AcpError, match="session/new returned no sessionId"):
        created_session_id(answer)


def test_no_config_selector_is_sent_without_a_session_or_a_model() -> None:
    assert config_option_requests(session_id=None, model="m", reasoning_effort=None) == ()
    assert config_option_requests(session_id="s", model=None, reasoning_effort=None) == ()


def test_the_model_selector_alone_is_sent_when_no_effort_is_configured() -> None:
    assert config_option_requests(session_id="s", model="deepseek-v4", reasoning_effort=None) == (
        {"sessionId": "s", "configId": "model", "value": "deepseek-v4"},
    )


def test_the_reasoning_effort_follows_the_model_selector() -> None:
    assert config_option_requests(session_id="s", model="m", reasoning_effort="high") == (
        {"sessionId": "s", "configId": "model", "value": "m"},
        {"sessionId": "s", "configId": "reasoning_effort", "value": "high"},
    )


def test_the_prompt_carries_one_text_block() -> None:
    assert prompt_params(session_id="s", text="hello") == {
        "sessionId": "s",
        "prompt": [{"type": "text", "text": "hello"}],
    }


def test_cancel_and_close_address_the_session_they_mean() -> None:
    assert session_ref("s") == {"sessionId": "s"}


def test_allow_mode_answers_with_the_allow_once_option() -> None:
    choice = permission_choice(mode="allow", request=request_with(("allow_once", "reject_once")))
    assert choice == PermissionChoice(option_id="allow_once", denial=None)


def test_an_allow_request_without_the_option_answers_nothing() -> None:
    choice = permission_choice(mode="allow", request=request_with(("reject_once",)))
    assert choice == PermissionChoice(option_id=None, denial=None)


@pytest.mark.parametrize("mode", ["ask", "deny"])
def test_every_other_mode_refuses_and_names_the_tool(mode: str) -> None:
    choice = permission_choice(mode=mode, request=request_with(("allow_once", "reject_once")))
    assert choice == PermissionChoice(option_id="reject_once", denial="write file")


def test_the_denial_names_the_tool_call_id_when_the_title_is_missing() -> None:
    choice = permission_choice(
        mode="ask", request=request_with(("reject_once",), tool_call={"toolCallId": "call-9"})
    )
    assert choice.denial == "call-9"


def test_the_denial_falls_back_to_a_plain_tool_label() -> None:
    choice = permission_choice(mode="ask", request=request_with(("reject_once",), tool_call={}))
    assert choice.denial == "tool"


def test_a_refusal_without_the_option_still_names_the_tool() -> None:
    choice = permission_choice(mode="ask", request=request_with((), tool_call={"title": "reads"}))
    assert choice == PermissionChoice(option_id=None, denial="reads")


def test_a_mode_outside_the_config_s_domain_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown permission_mode 'always'"):
        permission_choice(mode="always", request=request_with(("allow_once", "reject_once")))

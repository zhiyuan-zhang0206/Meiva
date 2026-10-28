from __future__ import annotations

import pytest

from base.lm.harness.acp.errors import AcpRpcError
from base.lm.harness.acp.frames import (
    Blank,
    Frame,
    NonJson,
    NotAnObject,
    decode_line,
    encode_frame,
    non_json_note,
    notification_frame,
    request_frame,
    stderr_text,
)
from base.lm.harness.acp.inbound import (
    AcpNotification,
    AskPermission,
    Refusal,
    Reply,
    ServerRequest,
    answer_server_request,
    classify,
    reply_outcome,
)
from base.lm.harness.acp.permission import (
    PermissionOption,
    PermissionRequest,
    parse_permission_request,
    permission_response,
)


def test_a_request_frame_carries_its_id_method_and_a_copy_of_the_params() -> None:
    params = {"a": 1}

    frame = request_frame(7, "session/new", params)
    params["a"] = 2

    assert frame == {"jsonrpc": "2.0", "id": 7, "method": "session/new", "params": {"a": 1}}
    assert "params" not in request_frame(8, "ping", None)


def test_a_notification_frame_has_no_id() -> None:
    assert notification_frame("session/cancel", {"sessionId": "s"}) == {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": "s"},
    }
    assert notification_frame("initialized", None) == {"jsonrpc": "2.0", "method": "initialized"}


def test_an_encoded_frame_is_one_utf8_line_that_decodes_back_to_the_frame() -> None:
    frame = {"jsonrpc": "2.0", "id": 1, "result": {"text": "naïve — ok"}}

    line = encode_frame(frame)

    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert "naïve — ok" in line.decode("utf-8")
    assert decode_line(line) == Frame(frame)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b"   \n", Blank()),
        (b"not json\n", NonJson("not json")),
        (b"[1, 2]\n", NotAnObject()),
        (b'{"id": 3}\n', Frame({"id": 3})),
        (b"\xff\xfe{\n", NonJson("��{")),
    ],
)
def test_a_stdout_line_decodes_to_what_it_carries(line: bytes, expected: object) -> None:
    assert decode_line(line) == expected


def test_a_frame_with_id_and_method_is_a_server_request() -> None:
    assert classify({"id": 5, "method": "session/request_permission", "params": {"k": 1}}) == (
        ServerRequest(5, "session/request_permission", {"k": 1})
    )


def test_a_frame_with_a_method_alone_is_a_notification_with_object_params_only() -> None:
    assert classify({"method": "session/update", "params": {"u": 1}}) == AcpNotification(
        "session/update", {"u": 1}
    )
    assert classify({"method": "session/update", "params": [1]}) == AcpNotification(
        "session/update", {}
    )


def test_a_frame_with_an_integer_id_alone_answers_a_request() -> None:
    assert classify({"id": 9, "result": {"ok": True}}) == Reply(9, {"ok": True}, None)
    assert classify({"id": 9, "result": "scalar"}) == Reply(9, {}, None)
    assert classify({"id": 9, "error": {"code": -32000, "message": "no"}}) == Reply(
        9, None, {"code": -32000, "message": "no"}
    )


@pytest.mark.parametrize("frame", [{}, {"result": {}}, {"id": "9", "result": {}}])
def test_a_frame_that_answers_nothing_is_dropped(frame: dict[str, object]) -> None:
    assert classify(frame) is None


def test_the_permission_request_is_decoded_with_defaults_for_missing_fields() -> None:
    request = parse_permission_request(
        {
            "sessionId": "s-1",
            "toolCall": {"title": "run"},
            "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}, "junk", {}],
        }
    )

    assert request.session_id == "s-1"
    assert request.tool_call == {"title": "run"}
    assert request.options == (
        PermissionOption("a", "Allow", "allow_once"),
        PermissionOption("", "", ""),
    )
    assert request.option("allow_once") == PermissionOption("a", "Allow", "allow_once")
    assert request.option("reject_once") is None


def test_a_permission_request_without_options_or_tool_call_still_decodes() -> None:
    assert parse_permission_request({}) == PermissionRequest("", {}, ())


def test_a_chosen_option_is_selected_and_anything_else_is_cancelled() -> None:
    options = (PermissionOption("a", "Allow", "allow_once"),)

    assert permission_response(3, "a", options)["result"] == {
        "outcome": {"outcome": "selected", "optionId": "a"}
    }
    for chosen in (None, "other"):
        assert permission_response(3, chosen, options)["result"] == {
            "outcome": {"outcome": "cancelled"}
        }
    assert permission_response(3, "a", options)["id"] == 3


def test_a_permission_request_is_asked_and_every_other_client_method_is_refused() -> None:
    asked = answer_server_request(
        ServerRequest(4, "session/request_permission", {"sessionId": "s", "options": []})
    )
    assert isinstance(asked, AskPermission) and asked.request_id == 4

    refused = answer_server_request(ServerRequest(5, "fs/read_text_file", {}))
    assert isinstance(refused, Refusal)
    assert refused.frame["id"] == 5
    error = refused.frame["error"]
    assert error["code"] == -32601 and "fs/read_text_file" in error["message"]
    assert isinstance(
        answer_server_request(ServerRequest(6, "session/request_permission", [1])), Refusal
    )


def test_a_reply_settles_its_request_with_the_result_or_the_error_named_after_the_method() -> None:
    assert reply_outcome(Reply(1, {"sessionId": "s"}, None), "session/new") == {"sessionId": "s"}
    assert reply_outcome(Reply(2, None, None), "session/close") == {}

    refusal = reply_outcome(
        Reply(3, None, {"code": -32602, "message": "a prompt is already in flight", "data": 7}),
        "session/prompt",
    )

    assert isinstance(refusal, AcpRpcError)
    assert (refusal.method, refusal.code, refusal.message, refusal.data) == (
        "session/prompt",
        -32602,
        "a prompt is already in flight",
        7,
    )
    assert "session/prompt failed with JSON-RPC error -32602" in str(refusal)
    bare = reply_outcome(Reply(4, None, {}), "x")
    assert isinstance(bare, AcpRpcError) and (bare.code, bare.message) == (-1, "")


def test_the_stderr_tail_keeps_a_bounded_note_of_non_json_stdout_and_decoded_stderr_lines() -> None:
    assert non_json_note("x" * 400) == "non-JSON stdout: " + "x" * 300
    assert stderr_text(b"oops \xff here\r\n") == "oops \ufffd here"

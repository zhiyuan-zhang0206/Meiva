from __future__ import annotations

import json

from base.lm.harness.codex.frames import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    JsonRpcError,
    Notification,
    Reply,
    ServerRequest,
    as_object,
    as_text,
    classify,
    encode,
    notification_frame,
    request_frame,
)


def test_a_request_frame_carries_its_id_method_and_params() -> None:
    assert request_frame(7, "thread/start", {"cwd": "/ws"}) == {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "thread/start",
        "params": {"cwd": "/ws"},
    }


def test_a_notification_frame_omits_the_params_it_was_not_given() -> None:
    assert notification_frame("initialized", None) == {"jsonrpc": "2.0", "method": "initialized"}
    assert notification_frame("x", {"a": 1}) == {
        "jsonrpc": "2.0",
        "method": "x",
        "params": {"a": 1},
    }


def test_a_frame_is_one_compact_line_and_a_non_ascii_payload_is_escaped() -> None:
    assert encode({"jsonrpc": "2.0", "id": 1, "params": {"a": [1, 2]}}) == (
        b'{"jsonrpc":"2.0","id":1,"params":{"a":[1,2]}}\n'
    )
    assert encode({"text": "\u00e9"}) == b'{"text":"\\u00e9"}\n'


def test_a_frame_with_an_id_and_a_method_is_a_request_of_the_servers() -> None:
    line = (
        b'{"jsonrpc":"2.0","id":3,"method":"item/commandExecution/requestApproval",'
        b'"params":{"command":"rm -rf build"}}'
    )
    assert classify(line) == ServerRequest(
        3, "item/commandExecution/requestApproval", {"command": "rm -rf build"}
    )


def test_a_frame_with_a_method_alone_is_a_notification() -> None:
    assert classify(
        b'{"jsonrpc":"2.0","method":"turn/started","params":{"turn":{"id":"t-1"}}}'
    ) == (Notification("turn/started", {"turn": {"id": "t-1"}}))


def test_an_integer_id_alone_is_a_reply_and_keeps_its_whole_frame() -> None:
    frame = {"jsonrpc": "2.0", "id": 9, "result": {"thread": {"id": "thread-1"}}}
    assert classify(json.dumps(frame).encode()) == Reply(9, frame)


def test_a_line_that_says_nothing_is_dropped() -> None:
    assert classify(b"not json") is None
    assert classify(b"") is None
    assert classify(b"[1, 2]") is None
    assert classify(b"null") is None
    assert classify(b'{"jsonrpc":"2.0","id":"abc","result":{}}') is None
    assert classify(b'{"jsonrpc":"2.0","id":true,"result":{}}') is None


def test_a_reply_settles_with_the_result_object_it_carries() -> None:
    assert Reply(1, {"id": 1, "result": {"turn": {"id": "t"}}}).outcome() == {"turn": {"id": "t"}}
    assert Reply(1, {"id": 1}).outcome() == {}


def test_a_reply_carries_the_refusal_it_answered_with() -> None:
    outcome = Reply(
        1,
        {
            "id": 1,
            "error": {"code": -32600, "message": "thread not found", "data": {"why": "gone"}},
        },
    ).outcome()
    assert isinstance(outcome, JsonRpcError)
    assert (outcome.code, outcome.message, outcome.data) == (
        -32600,
        "thread not found",
        {"why": "gone"},
    )
    assert str(outcome) == "app-server error -32600: thread not found"


def test_a_refusal_without_a_code_or_a_message_still_names_one() -> None:
    outcome = Reply(1, {"id": 1, "error": {"data": 5}}).outcome()
    assert isinstance(outcome, JsonRpcError)
    assert (outcome.code, outcome.message, outcome.data) == (-32000, "unknown", 5)


def test_a_server_request_is_answered_on_its_own_id() -> None:
    request = ServerRequest("req-7", "applyPatchApproval", {})
    assert request.answered({"decision": "approved"}) == {
        "jsonrpc": "2.0",
        "id": "req-7",
        "result": {"decision": "approved"},
    }
    assert request.refused() == {
        "jsonrpc": "2.0",
        "id": "req-7",
        "error": {"code": METHOD_NOT_FOUND, "message": "no handler"},
    }
    assert request.failed(RuntimeError("boom")) == {
        "jsonrpc": "2.0",
        "id": "req-7",
        "error": {"code": INTERNAL_ERROR, "message": "boom"},
    }


def test_as_object_and_as_text_read_only_what_they_name() -> None:
    assert as_object({"a": 1}) == {"a": 1}
    assert as_object([1, 2]) == {}
    assert as_object(None) == {}
    assert as_text("x") == "x"
    assert as_text(1) is None

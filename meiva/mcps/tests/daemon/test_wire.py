from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from meiva.mcps._wire import (
    call_response,
    encode_line,
    error_response,
    ping_response,
    tools_response,
)


def test_ping_is_answered_with_pong() -> None:
    assert ping_response(0) == {"id": 0, "ok": True, "result": "pong"}


def test_an_error_response_carries_the_request_id_and_the_text() -> None:
    assert error_response(3, "Unknown method: x") == {
        "id": 3,
        "ok": False,
        "error": "Unknown method: x",
    }
    assert error_response(None, "JSON parse error") == {
        "id": None,
        "ok": False,
        "error": "JSON parse error",
    }


def test_tools_keep_the_sdks_snake_case_schema_field_and_fill_empty_values() -> None:
    tools = [
        Tool(name="read_file", description="Read a file", input_schema={"type": "object"}),
        Tool(name="write_file", input_schema={}),
    ]
    assert tools_response(1, tools) == {
        "id": 1,
        "ok": True,
        "result": [
            {"name": "read_file", "description": "Read a file", "input_schema": {"type": "object"}},
            {"name": "write_file", "description": "", "input_schema": {}},
        ],
    }


def test_a_call_result_is_serialized_with_camel_case_wire_keys() -> None:
    result = CallToolResult(
        content=[
            TextContent(type="text", text="hi"),
            ImageContent(type="image", data="b64==", mime_type="image/png"),
        ],
        is_error=False,
        structured_content={"k": 1},
    )
    assert call_response(7, result) == {
        "id": 7,
        "ok": True,
        "result": {
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image", "data": "b64==", "mimeType": "image/png"},
            ],
            "isError": False,
            "structuredContent": {"k": 1},
        },
    }


def test_a_tool_error_is_carried_as_data() -> None:
    result = CallToolResult(content=[TextContent(type="text", text="denied")], is_error=True)
    response = call_response(1, result)
    assert response["ok"] is True
    assert response["result"] == {
        "content": [{"type": "text", "text": "denied"}],
        "isError": True,
        "structuredContent": None,
    }


def test_a_result_with_an_empty_content_list_serializes_an_empty_list() -> None:
    response = call_response(6, CallToolResult(content=[]))
    assert response["result"] == {"content": [], "isError": False, "structuredContent": None}


# meiva:note
# The SDK contract says every ContentBlock is a pydantic model; a block without model_dump means the
# SDK changed, and it must not become a legal-looking `unknown` block.
def test_a_content_block_that_is_not_a_pydantic_model_fails_fast() -> None:
    class BadBlock:
        pass

    # meiva:note
    # `model_construct` skips the validation a real SDK result went through, which is how a block
    # of a type the SDK did not declare could reach the daemon.
    result = CallToolResult.model_construct(content=[BadBlock()])
    with pytest.raises(TypeError, match="Unrecognized MCP content block type 'BadBlock'"):
        call_response(2, result)


def test_a_response_is_one_utf8_json_line() -> None:
    line = encode_line(ping_response("é"))
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"id": "é", "ok": True, "result": "pong"}
    assert "é".encode() in line

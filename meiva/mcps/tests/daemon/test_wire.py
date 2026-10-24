from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from meiva.mcps._wire import (
    call_response,
    encode_line,
    error_response,
    ping_response,
    tools_response,
)


# meiva:note
# An SDK-shaped result built by hand, for the shapes a real `CallToolResult` cannot take (no content at all, a block that is no pydantic model).
@dataclass(frozen=True)
class RawResult:
    content: list[object] | None
    is_error: bool | None = False
    structured_content: dict[str, object] | None = None


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


def test_a_result_without_content_serializes_an_empty_list() -> None:
    response = call_response(6, RawResult(content=None, is_error=None))
    assert response["result"] == {"content": [], "isError": False, "structuredContent": None}


# meiva:note
# The SDK contract says every ContentBlock is a pydantic model; a block without model_dump means the
# SDK changed, and it must not become a legal-looking `unknown` block.
def test_a_content_block_that_is_not_a_pydantic_model_fails_fast() -> None:
    class BadBlock:
        pass

    result = RawResult(content=[BadBlock()])
    with pytest.raises(TypeError, match="Unrecognized MCP content block type 'BadBlock'"):
        call_response(2, result)


def test_a_response_is_one_utf8_json_line() -> None:
    line = encode_line(ping_response("é"))
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"id": "é", "ok": True, "result": "pong"}
    assert "é".encode() in line

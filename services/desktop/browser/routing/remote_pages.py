# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from mcp.types import CallToolResult, TextContent

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

_SELECTED = re.compile(r"^\s*(\d+):.*\[selected\](?:\s+\S+=\S+)*\s*$", re.MULTILINE)
_PAGE_LINE = re.compile(r"^\s*(\d+): ")
REMOTE_TOOLS = frozenset(
    {
        "list_pages",
        "new_page",
        "navigate_page",
        "select_page",
        "close_page",
        "renew_page",
        "take_screenshot",
        "take_snapshot",
        "click",
        "click_at",
        "hover",
        "fill",
        "fill_form",
        "type_text",
        "drag",
        "press_key",
        "upload_file",
        "handle_dialog",
        "wait_for",
        "list_console_messages",
        "get_console_message",
        "list_network_requests",
        "get_network_request",
        "evaluate_script",
    }
)
PAGE_LIST_TOOLS = frozenset(
    {"list_pages", "new_page", "navigate_page", "select_page", "close_page"}
)
PAGE_SCOPED_TOOLS = REMOTE_TOOLS - {
    "list_pages",
    "new_page",
    "select_page",
    "close_page",
    "renew_page",
}


def require_remote_tool(tool: str, args: Mapping[str, object]) -> None:
    if tool not in REMOTE_TOOLS or args.get("serviceWorkerId") is not None:
        raise ValueError(f"{tool} is unavailable on remote Chrome")
    if tool == "take_screenshot" and (args.get("fullPage") or args.get("uid")):
        raise ValueError("Remote Chrome screenshots are limited to the visible viewport")


def listed_pages(result: CallToolResult) -> set[int]:
    return {
        int(match[1])
        for item in result.content
        if isinstance(item, TextContent)
        for line in item.text.splitlines()
        if (match := _PAGE_LINE.match(line)) is not None
    }


def structured_fields(result: CallToolResult) -> dict[str, object]:
    raw = object_of(result.model_dump, mode="json", by_alias=True, include={"structured_content"})
    fields = raw.get("structuredContent") if is_object_dict(raw) else None
    return fields if is_object_dict(fields) else {}


def reconnected(result: CallToolResult) -> bool:
    structured = structured_fields(result)
    return (structured.get("reconnected") is True) or any(
        isinstance(item, TextContent)
        and item.text.startswith(
            "Note: the browser was restarted or reconnected since the last call."
        )
        for item in result.content
    )


def page_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def authorize_page(tool: str, args: Mapping[str, object], owns: Callable[[int], bool]) -> None:
    require_remote_tool(tool, args)
    if (
        tool in {"navigate_page", "new_page"}
        and isinstance(url := args.get("url"), str)
        and url != "about:blank"
        and urlsplit(url).scheme not in {"http", "https"}
    ):
        raise ValueError("Remote Chrome navigation requires an HTTP(S) URL or about:blank")
    if "pageId" in args or tool in {"select_page", "close_page"}:
        page = page_number(args.get("pageId"))
        if page is None or not owns(page):
            raise ValueError("Remote Chrome page does not belong to this agent. Open a page first.")


def scope_page_list(
    result: CallToolResult, owns: Callable[[int], bool], selected: int | None
) -> CallToolResult:
    if result.is_error:
        return result
    content = list(result.content)
    for index, item in enumerate(content):
        if not isinstance(item, TextContent):
            continue
        lines: list[str] = []
        for line in item.text.splitlines():
            match = _PAGE_LINE.match(line)
            if match is None:
                lines.append(line)
                continue
            page = int(match[1])
            if owns(page):
                unselected = line.replace(" [selected]", "")
                lines.append(f"{unselected} [selected]" if page == selected else unselected)
        content[index] = item.model_copy(update={"text": "\n".join(lines)})
    return result.model_copy(update={"content": content, "structured_content": None})


def selected_page(result: CallToolResult) -> int | None:
    for item in result.content:
        if isinstance(item, TextContent) and (match := _SELECTED.search(item.text)) is not None:
            return int(match[1])
    return None

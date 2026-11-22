# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.types import CallToolResult, TextContent, Tool

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

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
    if tool == "take_snapshot" and "filePath" in args:
        raise ValueError(
            "Remote Chrome snapshot file export is unavailable. Omit filePath for an inline result."
        )
    if tool == "take_screenshot" and (args.get("fullPage") or args.get("uid")):
        raise ValueError("Remote Chrome screenshots are limited to the visible viewport")


def bound_page_id_required(tool: Tool) -> bool:
    required: object = tool.input_schema.get("required")
    return (
        tool.name not in {"select_page", "close_page"}
        and is_object_list(required)
        and "pageId" in required
    )


def public_page_schema(tool: Tool) -> Tool:
    if not bound_page_id_required(tool):
        return tool
    required: object = tool.input_schema.get("required")
    if not is_object_list(required):
        return tool
    schema = {**tool.input_schema, "required": [field for field in required if field != "pageId"]}
    return tool.model_copy(update={"input_schema": schema})


@dataclass(frozen=True)
class ListedPage:
    id: int
    url: str
    title: str
    selected: bool


def structured_pages(result: CallToolResult) -> dict[int, ListedPage]:
    if result.is_error:
        raise ValueError("SSH Chrome could not verify tab ownership")
    raw = structured_fields(result).get("pages")
    if not is_object_list(raw):
        raise ValueError("SSH Chrome returned invalid structured pages")
    pages: dict[int, ListedPage] = {}
    for entry in raw:
        if not is_object_dict(entry):
            raise ValueError("SSH Chrome returned invalid structured pages")
        pid, url, title, selected = (entry.get(key) for key in ("id", "url", "title", "selected"))
        if (
            type(pid) is not int
            or pid <= 0
            or pid in pages
            or not isinstance(url, str)
            or not isinstance(title, str)
            or type(selected) is not bool
        ):
            raise ValueError("SSH Chrome returned invalid structured pages")
        pages[pid] = ListedPage(pid, url, title, selected)
    return pages


def scoped_structured_page_list(
    pages: Mapping[int, ListedPage], owns: Callable[[int], bool], selected: int | None
) -> CallToolResult:
    lines = ["## Pages"]
    for pid, page in pages.items():
        if owns(pid):
            url = " ".join(page.url.splitlines())
            title = " ".join(page.title.splitlines())
            label = f"{title} ({url})" if title else url
            lines.append(f"{pid}: {label}{' [selected]' if pid == selected else ''}")
    return CallToolResult(content=[TextContent(type="text", text="\n".join(lines))])


def structured_fields(result: CallToolResult) -> dict[str, object]:
    fields = object_of(getattr, result, "structured_content")
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

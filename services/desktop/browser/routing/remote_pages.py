# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from mcp.types import CallToolResult, TextContent

_SELECTED = re.compile(r"^\s*(\d+):.*\[selected\](?:\s+\S+=\S+)*\s*$", re.MULTILINE)
_PAGE_LINE = re.compile(r"^\s*(\d+): ")
_UNSUPPORTED = frozenset(
    {
        "install_extension",
        "uninstall_extension",
        "list_extensions",
        "reload_extension",
        "trigger_extension_action",
        "screencast_start",
        "screencast_stop",
    }
)
PAGE_LIST_TOOLS = frozenset(
    {"list_pages", "new_page", "navigate_page", "select_page", "close_page"}
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
    if tool in _UNSUPPORTED or args.get("serviceWorkerId") is not None:
        raise ValueError(f"{tool} is unavailable on remote Chrome")
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
    return result.model_copy(update={"content": content})


def selected_page(result: CallToolResult) -> int | None:
    for item in result.content:
        if isinstance(item, TextContent) and (match := _SELECTED.search(item.text)) is not None:
            return int(match[1])
    return None

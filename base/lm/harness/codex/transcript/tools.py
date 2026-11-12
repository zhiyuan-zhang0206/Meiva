from __future__ import annotations

import json
from collections.abc import Callable

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.codex.transcript.errors import UnknownValueError
from base.lm.harness.codex.transcript.place import Place
from base.lm.harness.codex.transcript.scalars import opt_int, opt_text, text
from base.lm.harness.codex.transcript.shapes import Obj, opt_objects
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnyEntry,
    Notice,
    Reasoning,
    ToolCall,
    ToolResult,
    ToolStatus,
)

# meiva:note
# The thread items that are a tool, an edit or a piece of the agent's own output other than its plain
# messages and commands. A tool item is a call and, once it has finished, its result; an item that is still
# `inProgress` gives the call only and gains its result when the finished item arrives (the keys are the same). The
# tool names are the ones the adapter's live events already use (`patch`, `mcp:<server>/<tool>`, `web_search`, the
# dynamic tool's own name), so a timeline reads the same whichever path wrote it. A plan is the agent's reasoning
# aloud (it is shown as reasoning and never delivered as a reply). Items this module knows and gives no entry (a
# hook prompt, a function-call output, a sub-agent activity record) are bookkeeping of the model's context or a
# repeat of what the collab call already says; they are listed in `QUIET_ITEMS` so that they are not reported as
# unknown.
# meiva:link base/lm/harness/docs/transcript-projection.md

QUIET_ITEMS = frozenset({"hookPrompt", "functionCallOutput", "subAgentActivity"})

_PATCH_STATUS = {
    "completed": ToolStatus.OK,
    "failed": ToolStatus.ERROR,
    "declined": ToolStatus.DENIED,
}
_CALL_STATUS = {"completed": ToolStatus.OK, "failed": ToolStatus.ERROR}


def _quiet_item(_item: Obj, _place: Place) -> list[AnyEntry]:
    return []


def _pair(
    place: Place,
    name: str,
    tool_input: dict[str, object],
    result: tuple[str, ToolStatus, int | None] | None,
) -> list[AnyEntry]:
    call = ToolCall(**place.common("call"), call_id=place.id, name=name, tool_input=tool_input)
    if result is None:
        return [call]
    content, status, duration = result
    return [
        call,
        ToolResult(
            **place.common("result", 1, at_ms=place.completed_ms),
            call_id=place.id,
            content=content,
            status=status,
            duration_ms=place.elapsed_ms() if duration is None else duration,
        ),
    ]


def _status(item: Obj, known: dict[str, ToolStatus], what: str) -> ToolStatus | None:
    status = text(item, "status")
    if status == "inProgress":
        return None
    found = known.get(status)
    if found is None:
        raise UnknownValueError(f"{what}.status={status}")
    return found


def _joined(blocks: object, *, field: str) -> str | None:
    if not is_object_list(blocks):
        return None
    parts: list[str] = []
    for block in blocks:
        if not is_object_dict(block) or not isinstance(block.get(field), str):
            return None
        parts.append(str(block[field]))
    return "\n".join(parts)


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _file_change(item: Obj, place: Place) -> list[AnyEntry]:
    changes: list[dict[str, object]] = []
    for change in opt_objects(item, "changes"):
        path = opt_text(change, "path")
        if path is None:
            continue
        entry: dict[str, object] = {"path": path}
        kind = change.get("kind")
        label = kind.get("type") if is_object_dict(kind) else kind
        if isinstance(label, str):
            entry["kind"] = label
        if isinstance(diff := change.get("diff"), str):
            entry["diff"] = diff
        changes.append(entry)
    paths = [str(change["path"]) for change in changes]
    tool_input: dict[str, object] = {"paths": paths}
    if any("kind" in change or "diff" in change for change in changes):
        tool_input["changes"] = changes
    status = _status(item, _PATCH_STATUS, "fileChange")
    outcome = (
        None
        if status is None
        else (_dump({"changes": paths, "status": text(item, "status")}), status, None)
    )
    return _pair(place, "patch", tool_input, outcome)


def _arguments(value: object) -> dict[str, object]:
    return dict(value) if is_object_dict(value) else {"arguments": value}


def _mcp(item: Obj, place: Place) -> list[AnyEntry]:
    name = f"mcp:{text(item, 'server')}/{text(item, 'tool')}"
    status = _status(item, _CALL_STATUS, "mcpToolCall")
    if status is None:
        return _pair(place, name, _arguments(item.get("arguments")), None)
    error = item.get("error")
    result = item.get("result")
    if is_object_dict(error):
        body, failed = opt_text(error, "message") or _dump(error), True
    elif is_object_dict(result):
        body, failed = _joined(result.get("content"), field="text") or _dump(result), False
    else:
        body, failed = "", False
    final = ToolStatus.ERROR if failed else status
    return _pair(
        place, name, _arguments(item.get("arguments")), (body, final, opt_int(item, "durationMs"))
    )


def _dynamic(item: Obj, place: Place) -> list[AnyEntry]:
    status = _status(item, _CALL_STATUS, "dynamicToolCall")
    arguments = _arguments(item.get("arguments"))
    if status is None:
        return _pair(place, text(item, "tool"), arguments, None)
    body = _joined(item.get("contentItems"), field="text") or _dump(item.get("contentItems") or [])
    final = ToolStatus.ERROR if item.get("success") is False else status
    return _pair(place, text(item, "tool"), arguments, (body, final, opt_int(item, "durationMs")))


def _web_search(item: Obj, place: Place) -> list[AnyEntry]:
    summary: dict[str, object] = {"query": text(item, "query")}
    if item.get("action") is not None:
        summary["action"] = item["action"]
    if item.get("results") is not None:
        summary["results"] = item["results"]
    return _pair(
        place, "web_search", {"query": summary["query"]}, (_dump(summary), ToolStatus.OK, None)
    )


def _image_view(item: Obj, place: Place) -> list[AnyEntry]:
    return _pair(place, "view_image", {"path": text(item, "path")}, ("", ToolStatus.OK, None))


def _sleep(item: Obj, place: Place) -> list[AnyEntry]:
    return _pair(place, "sleep", {"durationMs": item.get("durationMs")}, ("", ToolStatus.OK, None))


def _image_generation(item: Obj, place: Place) -> list[AnyEntry]:
    status = text(item, "status")
    prompt: dict[str, object] = {}
    if (revised := opt_text(item, "revisedPrompt")) is not None:
        prompt["revisedPrompt"] = revised
    failure = item.get("failure")
    saved = opt_text(item, "savedPath")
    body = _dump(failure) if failure is not None else saved or ""
    ended = ToolStatus.ERROR if failure is not None else ToolStatus.OK
    if status in ("inProgress", "generating", "in_progress"):
        return _pair(place, "image_generation", prompt, None)
    return _pair(place, "image_generation", prompt, (body, ended, None))


def _plan(item: Obj, place: Place) -> list[AnyEntry]:
    return [
        Reasoning(**place.common("plan"), text=text(item, "text"), duration_ms=place.elapsed_ms())
    ]


def _entered_review(item: Obj, place: Place) -> list[AnyEntry]:
    return [
        Notice(
            **place.common("review-entered"),
            text=f"Entered review mode: {text(item, 'review')}",
            opens_turn=False,
        )
    ]


def _exited_review(item: Obj, place: Place) -> list[AnyEntry]:
    return [
        AgentText(**place.common("review-exited"), text=text(item, "review"), message_ref=place.id)
    ]


HANDLERS: dict[str, Callable[[Obj, Place], list[AnyEntry]]] = {
    "fileChange": _file_change,
    "mcpToolCall": _mcp,
    "dynamicToolCall": _dynamic,
    "webSearch": _web_search,
    "imageView": _image_view,
    "sleep": _sleep,
    "imageGeneration": _image_generation,
    "plan": _plan,
    "enteredReviewMode": _entered_review,
    "exitedReviewMode": _exited_review,
    **dict.fromkeys(QUIET_ITEMS, _quiet_item),
}

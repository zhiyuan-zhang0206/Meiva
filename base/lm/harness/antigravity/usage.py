from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.antigravity.acp import input_marker, received_markers
from base.lm.harness.events import Usage


def _tokens(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


@dataclass(frozen=True, slots=True)
class PromptUsage:
    input: int
    output: int
    cached_read: int
    cached_write: int
    thought: int

    @classmethod
    def from_result(cls, usage: object) -> PromptUsage | None:
        if not is_object_dict(usage):
            return None
        return cls(
            _tokens(usage.get("inputTokens")),
            _tokens(usage.get("outputTokens")),
            _tokens(usage.get("cachedReadTokens")),
            _tokens(usage.get("cachedWriteTokens")),
            _tokens(usage.get("thoughtTokens")),
        )

    def billed(self) -> Usage:
        return Usage(
            self.input + self.cached_read + self.cached_write,
            self.output + self.thought,
            self.cached_write,
            self.cached_read,
        )


_SENDER = re.compile(r"\bsender=(\S+)")


def planner_usage(
    rows: Sequence[dict[str, object]], keys: Sequence[str], final_text: str
) -> tuple[Usage, ...]:
    planner = _owned_planner_rows(rows, keys)
    if not planner or not final_text:
        return ()
    last_text = planner[-1].get("content")
    if not isinstance(last_text, str) or last_text.strip() != final_text.strip():
        return ()
    if not all(
        row.get("status") == "DONE" and type(row.get("input_tokens")) is int for row in planner
    ):
        return ()
    return tuple(_planner_tokens(row) for row in planner)


def _owned_planner_rows(
    rows: Sequence[dict[str, object]], keys: Sequence[str]
) -> list[dict[str, object]]:
    marks = {input_marker(key) for key in keys}
    found: set[str] = set()
    planner: list[dict[str, object]] = []
    after_last_input = False
    for row in rows:
        kind = row.get("type")
        if kind == "USER_INPUT":
            own = received_markers((row,)) & marks
            if (found and not own) or (found & own):
                return []
            found.update(own)
            after_last_input = False
        elif found and _background_notice(row):
            return []
        elif found and kind == "PLANNER_RESPONSE":
            planner.append(row)
            after_last_input = True
    if found != marks or not after_last_input:
        return []
    return planner


def _background_notice(row: dict[str, object]) -> bool:
    content = row.get("content")
    if row.get("type") != "SYSTEM_MESSAGE" or not isinstance(content, str):
        return False
    sender = _SENDER.search(content)
    return sender is not None and sender[1] != "system"


def _planner_tokens(row: dict[str, object]) -> Usage:
    cached = _tokens(row.get("cache_read_tokens"))
    return Usage(
        _tokens(row.get("input_tokens")) + cached,
        _tokens(row.get("output_tokens")),
        cache_read_input_tokens=cached,
    )


@dataclass(frozen=True, slots=True)
class TurnBilling:
    usage: Usage
    uncertain: bool = False


def turn_usage(
    reports: Sequence[PromptUsage],
    steps: Sequence[Usage],
    *,
    request_count: int,
    terminal_reported: bool,
) -> TurnBilling:
    if request_count == 1 and reports:
        return TurnBilling(reports[-1].billed())
    recorded = sum(steps, Usage())
    if steps and reports:
        if _visible(reports[-1]) == recorded:
            return TurnBilling(reports[-1].billed())
        if sum((_visible(report) for report in reports), Usage()) == recorded:
            return TurnBilling(sum((report.billed() for report in reports), Usage()))
    if terminal_reported or not steps:
        return TurnBilling(reports[-1].billed() if reports else Usage(), uncertain=True)
    return TurnBilling(recorded + _matched_extras(reports, steps), uncertain=True)


def _visible(report: PromptUsage) -> Usage:
    return Usage(
        report.input + report.cached_read, report.output, cache_read_input_tokens=report.cached_read
    )


def _matched_extras(reports: Sequence[PromptUsage], steps: Sequence[Usage]) -> Usage:
    cursor = 0
    extras = Usage()
    for report in reports:
        match = _match_usage(_visible(report), steps, cursor)
        if match is not None:
            cursor = match
            extras += Usage(report.cached_write, report.thought, report.cached_write)
    return extras


def _match_usage(target: Usage, steps: Sequence[Usage], cursor: int) -> int | None:
    for start in range(cursor, len(steps)):
        running = Usage()
        for end in range(start, len(steps)):
            running += steps[end]
            if running == target:
                return end + 1
            if running.input_tokens > target.input_tokens:
                break
    return None

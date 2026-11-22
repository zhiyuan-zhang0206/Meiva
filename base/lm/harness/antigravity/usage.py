from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
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


def planner_usage(rows: Sequence[dict[str, object]]) -> tuple[Usage, ...]:
    return tuple(
        Usage(
            _tokens(row.get("input_tokens")) + _tokens(row.get("cache_read_tokens")),
            _tokens(row.get("output_tokens")),
            cache_read_input_tokens=_tokens(row.get("cache_read_tokens")),
        )
        for row in rows
        if row.get("type") == "PLANNER_RESPONSE" and "input_tokens" in row
    )


def turn_usage(reports: Sequence[PromptUsage], steps: Sequence[Usage]) -> Usage:
    reported = sum((report.billed() for report in reports), Usage())
    if not steps:
        return reported
    recorded = sum(steps, Usage())
    if (
        reported.input_tokens - reported.cache_creation_input_tokens == recorded.input_tokens
        and reported.output_tokens - sum(report.thought for report in reports)
        == recorded.output_tokens
        and reported.cache_read_input_tokens == recorded.cache_read_input_tokens
    ):
        return reported
    for report in reversed(reports):
        if _match_usage(report, (recorded,), 0) is not None:
            return report.billed()
    cursor = 0
    extras = Usage()
    for report in reports:
        match = _match_usage(report, steps, cursor)
        if match is not None:
            cursor = match
            extras += Usage(report.cached_write, report.thought, report.cached_write)
    return recorded + extras


def _match_usage(report: PromptUsage, steps: Sequence[Usage], cursor: int) -> int | None:
    target = Usage(
        report.input + report.cached_read, report.output, cache_read_input_tokens=report.cached_read
    )
    for start in range(cursor, len(steps)):
        running = Usage()
        for end in range(start, len(steps)):
            running += steps[end]
            if running == target:
                return end + 1
            if running.input_tokens > target.input_tokens:
                break
    return None

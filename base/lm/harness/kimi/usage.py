from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# meiva:note
# ACP carries only an aggregate `usage_update` (`used`/`size` are context-window
# occupancy, not tokens billed this turn), so per-turn accounting reads Kimi
# Code's persisted session wire log under `<kimi_home>/sessions/<workspace>/<session>/`
# (`wire.jsonl` or `agents/main/wire.jsonl`). Each `StatusUpdate` (or
# `assistant/message`) carries per-step token counters (`input_other`, `output`,
# `input_cache_read`, `input_cache_creation`).
#
# Per harness invariant 6, `StepUsage.input_tokens` is the full prompt token
# count (`input_other + cache_read_tokens + cache_write_tokens`), matching
# `lm.pricing.quote()`. When the log or its counters are missing, the snapshot
# reports `per_turn_usage=False` with a reason instead of returning silent zeros.
# meiva:link base/lm/harness/kimi/docs/kimi.meiva.okf.md
DEBOUNCE_SECONDS = 0.0
LOG_NAMES = ("wire.jsonl", "agents/main/wire.jsonl", "session.jsonl")
_TURN_BOUNDARY_TYPES = frozenset({"TurnBegin", "turn_begin", "user/message"})
_USAGE_EVENT_TYPES = frozenset({"StatusUpdate", "status_update", "assistant/message"})


class UsageUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StepUsage:
    turn: int
    step: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    per_turn_usage: bool
    session_id: str
    steps: tuple[StepUsage, ...] = ()
    reason: str | None = None

    def for_turn(self, turn: int) -> tuple[StepUsage, ...]:
        return tuple(step for step in self.steps if step.turn == turn)

    def latest_turn(self) -> tuple[StepUsage, ...]:
        if not self.steps:
            return ()
        newest = max(step.turn for step in self.steps)
        return self.for_turn(newest)

    def totals(self) -> StepUsage:
        return StepUsage(
            turn=0,
            step=0,
            input_tokens=sum(step.input_tokens for step in self.steps),
            output_tokens=sum(step.output_tokens for step in self.steps),
            cache_read_tokens=sum(step.cache_read_tokens for step in self.steps),
            cache_write_tokens=sum(step.cache_write_tokens for step in self.steps),
        )


Decoder = Callable[[Path], bytes]


def session_log_dir(kimi_home: Path, cwd: Path) -> Path:
    escaped = f"-{Path(cwd).as_posix().replace('/', '-')}-"
    return Path(kimi_home) / "sessions" / escaped


def session_log_path(kimi_home: Path, cwd: Path, session_id: str) -> Path | None:
    directory = session_log_dir(kimi_home, cwd) / session_id
    for rel in LOG_NAMES:
        candidate = directory / rel
        if candidate.is_file():
            return candidate
    sessions_root = Path(kimi_home) / "sessions"
    for rel in LOG_NAMES:
        for candidate in sorted(sessions_root.glob(f"*/{session_id}/{rel}")):
            if candidate.is_file():
                return candidate
    return None


def read_usage(
    kimi_home: Path,
    cwd: Path,
    session_id: str,
    *,
    decoder: Decoder | None = None,
) -> UsageSnapshot:
    path = session_log_path(kimi_home, cwd, session_id)
    if path is None:
        return UsageSnapshot(
            per_turn_usage=False,
            session_id=session_id,
            reason=f"no session log under {kimi_home}/sessions for {session_id}",
        )
    try:
        payload = (decoder or _read_bytes)(path)
    except UsageUnavailableError as exc:
        return UsageSnapshot(per_turn_usage=False, session_id=session_id, reason=str(exc))
    try:
        steps = _parse(payload)
    except ValueError as exc:
        return UsageSnapshot(per_turn_usage=False, session_id=session_id, reason=str(exc))
    if not steps:
        return UsageSnapshot(
            per_turn_usage=False,
            session_id=session_id,
            reason=f"session log {path} carries no assistant usage yet",
        )
    return UsageSnapshot(per_turn_usage=True, session_id=session_id, steps=steps)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UsageUnavailableError(f"cannot read session log {path}: {exc}") from exc


def _parse(payload: bytes) -> tuple[StepUsage, ...]:
    steps: list[StepUsage] = []
    current_turn = 0
    step_in_turn = 0
    for number, raw in enumerate(payload.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"session log line {number} is not JSON") from exc
        if not isinstance(decoded, dict):
            continue
        event_type, data = _unwrap_event(cast("dict[str, Any]", decoded))
        if event_type in _TURN_BOUNDARY_TYPES:
            current_turn += 1
            step_in_turn = 0
            continue
        if event_type not in _USAGE_EVENT_TYPES:
            continue
        raw_usage = data.get("token_usage") or data.get("usage")
        if not isinstance(raw_usage, dict):
            continue
        if current_turn == 0:
            current_turn = 1
        step_in_turn += 1
        steps.append(
            _step(
                data,
                cast("dict[str, Any]", raw_usage),
                default_turn=current_turn,
                default_step=step_in_turn,
            )
        )
    return tuple(steps)


def _unwrap_event(record: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    nested = record.get("message")
    source = cast("dict[str, Any]", nested) if isinstance(nested, dict) else dict(record)
    event_type = str(source.get("type") or "")
    raw_data = source.get("data") or source.get("payload")
    data = cast("dict[str, Any]", raw_data) if isinstance(raw_data, dict) else source
    return event_type, data


def _step(
    data: Mapping[str, Any],
    usage: Mapping[str, Any],
    *,
    default_turn: int,
    default_step: int,
) -> StepUsage:
    turn = _int(data.get("turn")) or default_turn
    step = _int(data.get("step")) or default_step
    cache_read = _first_int(
        usage, "input_cache_read", "inputCacheRead", "cacheReadTokens", "cache_read_input_tokens"
    )
    cache_write = _first_int(
        usage,
        "input_cache_creation",
        "inputCacheCreation",
        "cacheWriteTokens",
        "cache_creation_input_tokens",
    )
    if "input_other" in usage or "inputOther" in usage:
        uncached = _first_int(usage, "input_other", "inputOther")
        input_tokens = uncached + cache_read + cache_write
    else:
        raw_input = _first_int(usage, "inputTokens", "input_tokens", "input")
        input_tokens = raw_input + cache_read + cache_write
    output_tokens = _first_int(usage, "output", "outputTokens", "output_tokens")
    return StepUsage(
        turn=turn,
        step=step,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def _first_int(mapping: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        if key in mapping:
            return _int(mapping[key])
    return 0


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def merge_snapshots(snapshots: Iterable[UsageSnapshot]) -> UsageSnapshot:
    merged: list[StepUsage] = []
    reasons: list[str] = []
    session_id = ""
    available = True
    for snapshot in snapshots:
        session_id = snapshot.session_id or session_id
        if snapshot.per_turn_usage:
            merged.extend(snapshot.steps)
        else:
            available = False
            if snapshot.reason:
                reasons.append(snapshot.reason)
    return UsageSnapshot(
        per_turn_usage=available,
        session_id=session_id,
        steps=tuple(merged),
        reason=None if available else "; ".join(reasons) or "usage unavailable",
    )

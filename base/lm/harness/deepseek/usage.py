from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# meiva:note
# ACP carries only an aggregate `usage_update` (`used`/`size` are context-window
# occupancy, not tokens billed this turn), so per-turn accounting reads the
# runtime's own persisted session log instead. That log is an internal format:
# `$DSH_HOME/sessions/<escaped cwd>/<session>/session.v4.jsonl.zstd`, zstd frames
# of one JSON event per line, and each `assistant/message` event carries the
# `usage` object the adapter needs.
#
# Reading an internal format is a risk we take deliberately and report: when the
# file, the decompressor, or the field is missing, the snapshot says
# `per_turn_usage=False` with a reason instead of returning zeros. An upper layer
# that silently received zeros would price a turn at nothing.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md
DEBOUNCE_SECONDS = 0.0
JSONL_SUFFIX = ".jsonl"
COMPRESSED_SUFFIX = ".jsonl.zstd"


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
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


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


def session_log_dir(dsh_home: Path, cwd: Path) -> Path:
    escaped = f"-{Path(cwd).as_posix().replace('/', '-')}-"
    return Path(dsh_home) / "sessions" / escaped


def session_log_path(dsh_home: Path, cwd: Path, session_id: str) -> Path | None:
    directory = session_log_dir(dsh_home, cwd) / session_id
    for suffix in (COMPRESSED_SUFFIX, JSONL_SUFFIX):
        candidate = directory / f"session.v4{suffix}"
        if candidate.is_file():
            return candidate
    for candidate in sorted(Path(dsh_home).glob(f"sessions/*/{session_id}/session.v4.*")):
        if candidate.is_file():
            return candidate
    return None


def read_usage(
    dsh_home: Path,
    cwd: Path,
    session_id: str,
    *,
    decoder: Decoder | None = None,
) -> UsageSnapshot:
    path = session_log_path(dsh_home, cwd, session_id)
    if path is None:
        return UsageSnapshot(
            per_turn_usage=False,
            session_id=session_id,
            reason=f"no session log under {dsh_home}/sessions for {session_id}",
        )
    try:
        payload = (decoder or decompress)(path)
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


def decompress(path: Path) -> bytes:
    if path.suffix == JSONL_SUFFIX:
        return path.read_bytes()
    try:
        import zstandard
    except ImportError:
        return _decompress_with_cli(path)
    try:
        with path.open("rb") as stream:
            return zstandard.ZstdDecompressor().stream_reader(stream).read()
    except zstandard.ZstdError as exc:
        raise UsageUnavailableError(f"{path} is not a readable zstd frame: {exc}") from exc


# meiva:note
# The zstd CLI is the fallback for a machine without the `zstandard` wheel: a
# missing decoder must surface as an unavailable snapshot with a reason, not as a
# silent zero. The argv is fixed here — no caller string reaches it.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md
def _decompress_with_cli(path: Path) -> bytes:
    try:
        completed = subprocess.run(  # noqa: S603
            ["zstd", "-dc", str(path)],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise UsageUnavailableError(
            "no zstd decoder: install the zstandard package or the zstd binary"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise UsageUnavailableError(f"zstd timed out reading {path}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
        raise UsageUnavailableError(f"zstd failed on {path} (rc={completed.returncode}): {detail}")
    return completed.stdout


def _parse(payload: bytes) -> tuple[StepUsage, ...]:
    steps: list[StepUsage] = []
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
        event = cast("dict[str, Any]", decoded)
        if event.get("type") != "assistant/message":
            continue
        raw_data = event.get("data")
        if not isinstance(raw_data, dict):
            continue
        data = cast("dict[str, Any]", raw_data)
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            continue
        steps.append(_step(data, cast("dict[str, Any]", raw_usage)))
    return tuple(steps)


def _step(data: Mapping[str, Any], usage: Mapping[str, Any]) -> StepUsage:
    return StepUsage(
        turn=_int(data.get("turn")),
        step=_int(data.get("step")),
        input_tokens=_int(usage.get("inputTokens")),
        output_tokens=_int(usage.get("outputTokens")),
        cache_read_tokens=_int(usage.get("cacheReadTokens")),
        cache_write_tokens=_int(usage.get("cacheWriteTokens")),
    )


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

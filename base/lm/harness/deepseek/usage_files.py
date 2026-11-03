from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import zstandard

from base.lm.harness.deepseek.usage import (
    JSONL_SUFFIX,
    UsageSnapshot,
    UsageUnavailableError,
    expected_log_paths,
    missing_log,
    parse_usage,
    unavailable,
)

# meiva:note
# The reads behind a session's usage: find its log, decode it (zstd or plain JSONL),
# and hand the bytes to the pure `parse_usage`. A missing or unreadable log is an
# unavailable snapshot with a reason, never zeros.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md


def _session_log_candidates(dsh_home: Path, cwd: Path, session_id: str) -> Iterable[Path]:
    yield from expected_log_paths(dsh_home, cwd, session_id)
    yield from sorted(Path(dsh_home).glob(f"sessions/*/{session_id}/session.v4.*"))


def session_log_path(dsh_home: Path, cwd: Path, session_id: str) -> Path | None:
    return next(
        (
            candidate
            for candidate in _session_log_candidates(dsh_home, cwd, session_id)
            if candidate.is_file()
        ),
        None,
    )


def read_usage(dsh_home: Path, cwd: Path, session_id: str) -> UsageSnapshot:
    path = session_log_path(dsh_home, cwd, session_id)
    if path is None:
        return missing_log(dsh_home, session_id)
    try:
        payload = decompress(path)
    except UsageUnavailableError as exc:
        return unavailable(session_id, str(exc))
    return parse_usage(payload, session_id=session_id, path=path)


def decompress(path: Path) -> bytes:
    if path.suffix == JSONL_SUFFIX:
        return path.read_bytes()
    try:
        with path.open("rb") as stream:
            return zstandard.ZstdDecompressor().stream_reader(stream).read()
    except zstandard.ZstdError as exc:
        raise UsageUnavailableError(f"{path} is not a readable zstd frame: {exc}") from exc

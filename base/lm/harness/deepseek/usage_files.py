from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

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
# The reads behind a session's usage: find its log, decode it (zstd, with the CLI as the fallback
# when the wheel is absent), and hand the bytes to the pure `parse_usage`. A missing log or decoder
# is an unavailable snapshot with a reason, never zeros.
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

from __future__ import annotations

from pathlib import Path

from base.host.boundary.jsonl import read_jsonl_rows
from base.host.env.harness_binaries import resolve_user_home


def transcript_path(conversation_id: str, *, home: Path | None = None) -> Path | None:
    log_dir = (
        resolve_user_home(home)
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
    )
    for filename in ("transcript_full.jsonl", "transcript.jsonl"):
        path = log_dir / filename
        if path.is_file():
            return path
    return None


def transcript_rows(conversation_id: str, *, home: Path | None = None) -> list[dict[str, object]]:
    path = transcript_path(conversation_id, home=home)
    if path is None:
        return []
    try:
        return read_jsonl_rows(path)
    except (OSError, ValueError):
        return []

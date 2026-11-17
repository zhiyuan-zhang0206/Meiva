"""Host timer arguments and agent-directory parsing for a strict scheduled attempt."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta


def run_at(spec: str, now: datetime) -> datetime:
    relative = re.fullmatch(r"\+(\d+)([smhd])", spec)
    if relative:
        count, unit = relative.groups()
        moment = now + timedelta(
            seconds=int(count) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        )
    else:
        moment = datetime.fromisoformat(spec)
    if moment.tzinfo is None or moment <= now:
        raise ValueError("--at must name a future time with timezone")
    return moment.astimezone(UTC)


def initiator_id(source: str) -> int | None:
    if source == "user":
        return None
    if not re.fullmatch(r"agent:[1-9][0-9]*", source):
        raise ValueError("--source must be user or agent:<positive id>")
    return int(source.split(":")[1])


def directory_page(text: str) -> tuple[list[int], int | None]:
    running: list[int] = []
    cursor = None
    for line in text.splitlines():
        if line.strip() == "(no agents)" or line.split()[:2] == ["id", "status"]:
            continue
        more = re.fullmatch(r"\(more agents: repeat with --before-id (\d+)\)", line)
        if more:
            cursor = int(more[1])
            continue
        row = re.match(r"\s*(\d+)\s+(running|idling|terminated)\s+\S+", line)
        if not row:
            raise ValueError(f"unreadable agent directory: {line!r}")
        if row[2] == "running":
            running.append(int(row[1]))
    if not text.strip():
        raise ValueError("empty agent directory")
    return running, cursor

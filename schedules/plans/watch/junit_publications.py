# meiva:note
# Both JUnit readers use the same publication boundary. GitHub artifacts are
# listed before the current run metadata is read: a changed workflow attempt
# then invalidates the old snapshot instead of supplying an unproved green.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

JUNIT_ARTIFACT_PREFIXES = ("test-results-", "verdict-junit-")


def attempt_started_at(metadata: Mapping[str, object], expected: int) -> datetime:
    if metadata.get("run_attempt") != expected:
        raise ValueError("workflow attempt metadata does not match the reviewed attempt")
    value = metadata.get("run_started_at")
    if not isinstance(value, str):
        raise TypeError("workflow attempt has no start timestamp")
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("workflow attempt timestamp has no timezone")
    return point


def current_publications(
    artifacts: Sequence[Mapping[str, object]],
    cutoff: datetime,
) -> tuple[dict[str, int], tuple[str, ...]]:
    latest: dict[str, tuple[datetime, int]] = {}
    issues: dict[str, str] = {}
    ambiguous: set[str] = set()
    for artifact in artifacts:
        name, identifier, created = (
            artifact.get("name"),
            artifact.get("id"),
            artifact.get("created_at"),
        )
        if not isinstance(name, str) or not name.startswith(JUNIT_ARTIFACT_PREFIXES):
            continue
        try:
            point = (
                datetime.fromisoformat(created.replace("Z", "+00:00"))
                if isinstance(created, str)
                else None
            )
        except ValueError:
            point = None
        if (
            not isinstance(identifier, int)
            or artifact.get("expired") is True
            or point is None
            or point.tzinfo is None
            or point < cutoff
        ):
            issues[name] = f"{name}: publication cannot be tied to the current workflow attempt"
            continue
        previous = latest.get(name)
        if previous is None or point > previous[0]:
            latest[name] = (point, identifier)
            ambiguous.discard(name)
        elif point == previous[0] and identifier != previous[1]:
            ambiguous.add(name)
    selected = {
        name: identifier for name, (_, identifier) in latest.items() if name not in ambiguous
    }
    errors = [detail for name, detail in issues.items() if name not in selected]
    errors.extend(
        f"{name}: ambiguous publications at the same timestamp" for name in sorted(ambiguous)
    )
    return selected, tuple(errors)

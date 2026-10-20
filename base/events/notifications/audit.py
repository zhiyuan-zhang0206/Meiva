# meiva:note
# The audit replays the producers rather than the delivery log: the log holds only
# what was delivered, so a switched-off category has no rows to count, and a switch
# cannot be simulated from the rows its own policy dropped. The gateway serves the
# resulting report; `meiva notices audit` only renders it.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg_pool import ConnectionPool

from base.events.notifications.catalog import NotificationCategory
from base.events.notifications.notices import notice_dedupe_key
from base.events.notifications.policy import SuppressionReason, decide
from base.events.notifications.store import read_overrides_sync

_NEEDS_INPUT_SQL = """
SELECT id, agent_id, created_at, updated_at, question
  FROM agent_notices
 WHERE require_response
   AND (created_at >= %s AND created_at < %s OR updated_at >= %s AND updated_at < %s)
 ORDER BY created_at, id
"""

_SIMULATED_SWITCHES: tuple[str, ...] = ("requestion",)


@dataclass(frozen=True)
class Candidate:
    category: str
    at: datetime
    agent_id: int
    dedupe_key: str | None


def _no_counts() -> Counter[str]:
    return Counter()


@dataclass(frozen=True)
class Explained:
    candidate: Candidate
    outcome: str


@dataclass(frozen=True)
class Replay:
    delivered: tuple[Candidate, ...]
    switched_off: Counter[str] = field(default_factory=_no_counts)
    deduplicated: Counter[str] = field(default_factory=_no_counts)
    explained: tuple[Explained, ...] = ()

    @property
    def candidates(self) -> int:
        return (
            len(self.delivered) + sum(self.switched_off.values()) + sum(self.deduplicated.values())
        )

    def suppressed(self, category: str) -> int:
        return self.switched_off[category] + self.deduplicated[category]


def notice_key(notice_id: int, question: object, *, requestion: bool) -> str:
    if requestion:
        return notice_dedupe_key(notice_id, question)
    return f"notice:{notice_id}"


@dataclass(frozen=True)
class _Query:
    start: datetime
    end: datetime
    requestion: bool


def _load_needs_input(cur: psycopg.Cursor[Any], query: _Query) -> list[Candidate]:
    cur.execute(_NEEDS_INPUT_SQL, (query.start, query.end, query.start, query.end))
    found: list[Candidate] = []
    for notice_id, agent_id, created_at, updated_at, question in cur.fetchall():
        found.extend(
            notice_candidates(
                notice_id=int(notice_id),
                agent_id=int(agent_id),
                created_at=created_at,
                updated_at=updated_at,
                question=question,
                start=query.start,
                end=query.end,
                requestion=query.requestion,
            )
        )
    return found


@dataclass(frozen=True)
class _Source:
    category: str
    origin: str
    load: Callable[[psycopg.Cursor[Any], _Query], list[Candidate]]


_SOURCES: tuple[_Source, ...] = (
    _Source(
        "core.needs_input",
        "agent_notices (require_response post and edit)",
        _load_needs_input,
    ),
)

MODELLED_CATEGORIES: tuple[str, ...] = tuple(source.category for source in _SOURCES)


def replay(
    candidates: Iterable[Candidate],
    *,
    categories: Mapping[str, NotificationCategory],
    switches: Mapping[str, bool],
    explain: bool = False,
) -> Replay:
    last_delivered: dict[tuple[str, str], datetime] = {}
    delivered: list[Candidate] = []
    switched_off: Counter[str] = Counter()
    deduplicated: Counter[str] = Counter()
    explained: list[Explained] = []
    for candidate in sorted(candidates, key=lambda item: (item.at, item.category, item.agent_id)):
        declared = categories[candidate.category]
        previous = (
            None
            if candidate.dedupe_key is None
            else last_delivered.get((candidate.category, candidate.dedupe_key))
        )
        decision = decide(declared, overrides=switches, last_same_key_at=previous, now=candidate.at)
        if not decision.deliver:
            if decision.reason is SuppressionReason.DISABLED:
                switched_off[candidate.category] += 1
                outcome = "off"
            else:
                deduplicated[candidate.category] += 1
                waited = (
                    "" if previous is None else f" {(candidate.at - previous).total_seconds():.0f}s"
                )
                outcome = f"duplicate{waited}"
            if explain:
                explained.append(Explained(candidate, outcome))
            continue
        delivered.append(candidate)
        if explain:
            explained.append(Explained(candidate, "delivered"))
        if candidate.dedupe_key is not None:
            last_delivered[(candidate.category, candidate.dedupe_key)] = candidate.at
    return Replay(tuple(delivered), switched_off, deduplicated, tuple(explained))


def load_candidates(
    pool: ConnectionPool[Any],
    *,
    start: datetime,
    end: datetime,
    categories: Sequence[str],
    requestion: bool,
) -> list[Candidate]:
    wanted = set(categories)
    query = _Query(start=start, end=end, requestion=requestion)
    found: list[Candidate] = []
    with pool.connection() as conn, conn.cursor() as cur:
        for source in _SOURCES:
            if source.category in wanted:
                found.extend(source.load(cur, query))
    return found


# meiva:note
# One notice row becomes one candidate, or two when the agent added a question after
# posting: the spine keys a re-ask by the question the notice carries now, so the
# post is keyed question-less and the edit by its question. Under the simulated
# `notice:<id>` key both collapse into one, which is the switch under audit.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def notice_candidates(
    *,
    notice_id: int,
    agent_id: int,
    created_at: datetime,
    updated_at: datetime | None,
    question: object,
    start: datetime,
    end: datetime,
    requestion: bool,
) -> list[Candidate]:
    edited_at = updated_at if updated_at is not None and updated_at > created_at else None
    asked_later = question is not None and edited_at is not None
    found: list[Candidate] = []
    if start <= created_at < end:
        found.append(
            Candidate(
                "core.needs_input",
                created_at,
                agent_id,
                notice_key(notice_id, None if asked_later else question, requestion=requestion),
            )
        )
    if asked_later and edited_at is not None and start <= edited_at < end:
        found.append(
            Candidate(
                "core.needs_input",
                edited_at,
                agent_id,
                notice_key(notice_id, question, requestion=requestion),
            )
        )
    return found


def per_day(
    delivered: Iterable[Candidate], *, start: datetime, end: datetime, tz: tzinfo
) -> list[tuple[date, Counter[str]]]:
    counts: dict[date, Counter[str]] = {}
    for candidate in delivered:
        counts.setdefault(candidate.at.astimezone(tz).date(), Counter())[candidate.category] += 1
    rows: list[tuple[date, Counter[str]]] = []
    day = start.astimezone(tz).date()
    last = (end - timedelta(microseconds=1)).astimezone(tz).date()
    while day <= last:
        rows.append((day, counts.get(day, Counter())))
        day += timedelta(days=1)
    return rows


def per_hour(delivered: Iterable[Candidate], *, tz: tzinfo) -> list[int]:
    buckets = [0] * 24
    for candidate in delivered:
        buckets[candidate.at.astimezone(tz).hour] += 1
    return buckets


def effective_switches(
    categories: Iterable[NotificationCategory], overrides: Mapping[str, bool]
) -> dict[str, bool]:
    return {
        category.id: overrides.get(category.id, category.default_enabled) for category in categories
    }


def _zone(name: str) -> tuple[tzinfo, str]:
    try:
        return ZoneInfo(name), name
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown time zone: {name}") from None


def parse_simulation(spec: str) -> tuple[str, bool]:
    name, _, value = spec.partition("=")
    if name not in _SIMULATED_SWITCHES or value not in ("on", "off"):
        raise ValueError(
            f"unknown simulation {spec!r}; expected "
            + " or ".join(f"{switch}=on|off" for switch in _SIMULATED_SWITCHES)
        )
    return name, value == "on"


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    days: int
    tz: tzinfo
    label: str


def window_for(*, days: int, tz_name: str, now: datetime | None) -> Window:
    if days < 1:
        raise ValueError("days must be at least 1")
    tz, label = _zone(tz_name)
    end = now or datetime.now(UTC)
    return Window(start=end - timedelta(days=days), end=end, days=days, tz=tz, label=label)


def _selection(
    *, category: str | None, simulate: Sequence[str]
) -> tuple[list[str], list[tuple[str, bool]]]:
    selected = [category] if category else list(MODELLED_CATEGORIES)
    if missing := [name for name in selected if name not in MODELLED_CATEGORIES]:
        raise ValueError(
            f"no candidate source for {missing[0]}; "
            f"modelled categories: {', '.join(MODELLED_CATEGORIES)}"
        )
    simulations = [parse_simulation(spec) for spec in simulate]
    needs = {"requestion": "core.needs_input"}
    for name, _ in simulations:
        if needs[name] not in selected:
            raise ValueError(f"simulate {name} needs category {needs[name]}")
    return selected, simulations


def _variants(
    *,
    simulations: Sequence[tuple[str, bool]],
    selected: Sequence[str],
    window: Window,
    pool: ConnectionPool[Any],
    catalog: Mapping[str, NotificationCategory],
    overrides: Mapping[str, bool],
    baseline: Replay,
) -> list[tuple[str, Replay]]:
    variants: list[tuple[str, Replay]] = [("baseline", baseline)]
    for _name, enabled in simulations:
        rekeyed = load_candidates(
            pool,
            start=window.start,
            end=window.end,
            categories=selected,
            requestion=enabled,
        )
        result = replay(rekeyed, categories=catalog, switches=overrides)
        variants.append((f"requestion={_on_off(value=enabled)}", result))
    return variants


def build_report(
    *,
    window: Window,
    categories: Sequence[NotificationCategory],
    switches: Mapping[str, bool],
    baseline: Replay,
    variants: Sequence[tuple[str, Replay]],
) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    per_day_rows: list[dict[str, Any]] = []
    for day, counts in per_day(
        baseline.delivered, start=window.start, end=window.end, tz=window.tz
    ):
        totals.update(counts)
        per_day_rows.append(
            {
                "day": day.isoformat(),
                "by_category": {category.id: counts[category.id] for category in categories},
                "total": sum(counts.values()),
            }
        )
    candidates_rows: list[dict[str, Any]] = []
    for category in categories:
        delivered = [c for c in baseline.delivered if c.category == category.id]
        keys = {c.dedupe_key for c in delivered if c.dedupe_key is not None}
        candidates_rows.append(
            {
                "category": category.id,
                "candidates": len(delivered) + baseline.suppressed(category.id),
                "delivered": len(delivered),
                "suppressed": baseline.suppressed(category.id),
                "repeat_deliveries": len(delivered) - len(keys),
            }
        )
    base_total = len(baseline.delivered)
    origins = {source.category: source.origin for source in _SOURCES}
    report: dict[str, Any] = {
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "days": window.days,
            "timezone": window.label,
        },
        "switches": dict(switches),
        "categories": [category.id for category in categories],
        "sources": {category.id: origins[category.id] for category in categories},
        "delivered": {
            "total": base_total,
            "per_day": per_day_rows,
            "per_category": {category.id: totals[category.id] for category in categories},
            "per_hour": per_hour(baseline.delivered, tz=window.tz),
        },
        "candidates": candidates_rows,
        "simulations": [
            {
                "variant": name,
                "delivered": len(result.delivered),
                "delta": len(result.delivered) - base_total,
                "per_day": round(len(result.delivered) / window.days, 3),
            }
            for name, result in variants
        ],
    }
    if baseline.explained:
        report["explain"] = explained_rows(baseline.explained, tz=window.tz)
    return report


def explained_rows(items: Sequence[Explained], *, tz: tzinfo) -> list[dict[str, Any]]:
    return [
        {
            "at": item.candidate.at.astimezone(tz).isoformat(),
            "category": item.candidate.category,
            "agent_id": item.candidate.agent_id,
            "dedupe_key": item.candidate.dedupe_key,
            "outcome": item.outcome,
        }
        for item in items
    ]


def _on_off(*, value: bool) -> str:
    return "on" if value else "off"


# meiva:note
# Replay the window's candidates through the delivery policy and return the report.
#
# Raises ValueError for an input the caller got wrong (window, zone, category, simulation);
# `catalog` is the merged declared categories the spine would honor right now.
def run_audit(
    pool: ConnectionPool[Any],
    *,
    catalog: Mapping[str, NotificationCategory],
    days: int,
    category: str | None,
    tz_name: str,
    now: datetime | None,
    simulate: Sequence[str],
    explain: bool,
) -> dict[str, Any]:
    window = window_for(days=days, tz_name=tz_name, now=now)
    selected, simulations = _selection(category=category, simulate=simulate)
    if missing := [name for name in selected if name not in catalog]:
        raise ValueError(f"category not declared by any plugin: {missing[0]}")
    declared = [catalog[name] for name in selected]
    overrides = read_overrides_sync(pool)
    candidates = load_candidates(
        pool, start=window.start, end=window.end, categories=selected, requestion=True
    )
    baseline = replay(candidates, categories=catalog, switches=overrides, explain=explain)
    return build_report(
        window=window,
        categories=declared,
        switches=effective_switches(declared, overrides),
        baseline=baseline,
        variants=_variants(
            simulations=simulations,
            selected=selected,
            window=window,
            pool=pool,
            catalog=catalog,
            overrides=overrides,
            baseline=baseline,
        ),
    )

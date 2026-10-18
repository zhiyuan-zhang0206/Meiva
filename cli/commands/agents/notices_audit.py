# meiva:note
# The audit replays the producers rather than the delivery log: the log holds only
# what was delivered, so a switched-off category has no rows to count, and a switch
# cannot be simulated from the rows its own policy dropped.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base.agents.context.clients import DatabaseFactory
from base.events.notifications.catalog import NotificationCategory, core_catalog
from base.events.notifications.notices import notice_dedupe_key
from base.events.notifications.policy import SuppressionReason, decide
from base.events.notifications.store import read_overrides_sync

MODELLED_CATEGORIES: tuple[str, ...] = ("core.needs_input", "core.turn_finished")

_CLEAN_HALT_BODIES: tuple[str, ...] = ("no tool_call (idle)", "system_halt (compact)")

_TURN_FINISHED_SQL = """
SELECT ts, agent_id
  FROM telemetry_events
 WHERE event_name = 'halt'
   AND attributes->>'body' = ANY(%s)
   AND ts >= %s AND ts < %s
 ORDER BY ts, id
"""

_NEEDS_INPUT_SQL = """
SELECT id, agent_id, created_at, updated_at, question
  FROM agent_notices
 WHERE require_response
   AND (created_at >= %s AND created_at < %s OR updated_at >= %s AND updated_at < %s)
 ORDER BY created_at, id
"""

_SIMULATED_SWITCHES: tuple[str, ...] = ("turn_finished", "requestion")

_SOURCES: dict[str, str] = {
    "core.needs_input": "core.needs_input    agent_notices (require_response post and edit)",
    "core.turn_finished": (
        "core.turn_finished  telemetry_events 'halt' (a turn that ended cleanly)"
    ),
}


@dataclass(frozen=True)
class Candidate:
    category: str
    at: datetime
    agent_id: int
    dedupe_key: str | None


def _no_counts() -> Counter[str]:
    return Counter()


@dataclass(frozen=True)
class Replay:
    delivered: tuple[Candidate, ...]
    switched_off: Counter[str] = field(default_factory=_no_counts)
    deduplicated: Counter[str] = field(default_factory=_no_counts)

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


def replay(
    candidates: Iterable[Candidate],
    *,
    categories: Mapping[str, NotificationCategory],
    switches: Mapping[str, bool],
) -> Replay:
    last_delivered: dict[tuple[str, str], datetime] = {}
    delivered: list[Candidate] = []
    switched_off: Counter[str] = Counter()
    deduplicated: Counter[str] = Counter()
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
            else:
                deduplicated[candidate.category] += 1
            continue
        delivered.append(candidate)
        if candidate.dedupe_key is not None:
            last_delivered[(candidate.category, candidate.dedupe_key)] = candidate.at
    return Replay(tuple(delivered), switched_off, deduplicated)


def load_candidates(
    database_factory: DatabaseFactory,
    *,
    start: datetime,
    end: datetime,
    categories: Sequence[str],
    requestion: bool,
) -> list[Candidate]:
    wanted = set(categories)
    found: list[Candidate] = []
    with database_factory().connect(autocommit=True) as conn, conn.cursor() as cur:
        if "core.turn_finished" in wanted:
            cur.execute(_TURN_FINISHED_SQL, (list(_CLEAN_HALT_BODIES), start, end))
            found.extend(
                Candidate("core.turn_finished", at, int(agent_id or 0), None)
                for at, agent_id in cur.fetchall()
            )
        if "core.needs_input" in wanted:
            cur.execute(_NEEDS_INPUT_SQL, (start, end, start, end))
            for notice_id, agent_id, created_at, updated_at, question in cur.fetchall():
                found.extend(
                    notice_candidates(
                        notice_id=int(notice_id),
                        agent_id=int(agent_id),
                        created_at=created_at,
                        updated_at=updated_at,
                        question=question,
                        start=start,
                        end=end,
                        requestion=requestion,
                    )
                )
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


def _zone(name: str | None) -> tuple[tzinfo, str]:
    if name is None:
        local = datetime.now().astimezone().tzinfo or UTC
        return local, str(local)
    try:
        return ZoneInfo(name), name
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown time zone: {name}") from None


def _table(headers: Sequence[object], rows: Sequence[Sequence[object]]) -> list[str]:
    cells = [[str(cell) for cell in row] for row in rows]
    titles = [str(header) for header in headers]
    widths = [len(title) for title in titles]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(title.ljust(widths[i]) for i, title in enumerate(titles)).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in cells:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


def _bar(value: int, peak: int, width: int = 40) -> str:
    if value == 0 or peak == 0:
        return ""
    return "#" * max(1, round(value * width / peak))


def parse_simulation(spec: str) -> tuple[str, bool]:
    name, _, value = spec.partition("=")
    if name not in _SIMULATED_SWITCHES or value not in ("on", "off"):
        raise ValueError(
            f"unknown simulation {spec!r}; expected "
            + " or ".join(f"{switch}=on|off" for switch in _SIMULATED_SWITCHES)
        )
    return name, value == "on"


@dataclass(frozen=True)
class _Window:
    start: datetime
    end: datetime
    days: int
    tz: tzinfo
    label: str


def _window(*, days: int, tz_name: str | None, now: datetime | None) -> _Window:
    if days < 1:
        raise ValueError("--days must be at least 1")
    tz, label = _zone(tz_name)
    end = now or datetime.now(UTC)
    return _Window(start=end - timedelta(days=days), end=end, days=days, tz=tz, label=label)


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
    needs = {"turn_finished": "core.turn_finished", "requestion": "core.needs_input"}
    for name, _ in simulations:
        if needs[name] not in selected:
            raise ValueError(f"--simulate {name} needs --category {needs[name]}")
    return selected, simulations


def _variants(
    *,
    simulations: Sequence[tuple[str, bool]],
    selected: Sequence[str],
    window: _Window,
    database_factory: DatabaseFactory,
    catalog: Mapping[str, NotificationCategory],
    overrides: Mapping[str, bool],
    candidates: Sequence[Candidate],
    baseline: Replay,
) -> list[tuple[str, Replay]]:
    variants: list[tuple[str, Replay]] = [("baseline", baseline)]
    for name, enabled in simulations:
        if name == "turn_finished":
            switched = dict(overrides) | {"core.turn_finished": enabled}
            result = replay(candidates, categories=catalog, switches=switched)
            variants.append((f"core.turn_finished={_on_off(value=enabled)}", result))
            continue
        rekeyed = load_candidates(
            database_factory,
            start=window.start,
            end=window.end,
            categories=selected,
            requestion=enabled,
        )
        result = replay(rekeyed, categories=catalog, switches=overrides)
        variants.append((f"requestion={_on_off(value=enabled)}", result))
    return variants


def _per_day_section(
    *,
    window: _Window,
    categories: Sequence[NotificationCategory],
    baseline: Replay,
) -> tuple[list[str], Counter[str]]:
    table: list[list[object]] = [["day", *(category.id for category in categories), "total"]]
    totals: Counter[str] = Counter()
    for day, counts in per_day(
        baseline.delivered, start=window.start, end=window.end, tz=window.tz
    ):
        totals.update(counts)
        table.append([day.isoformat(), *(counts[c.id] for c in categories), sum(counts.values())])
    table.append(["total", *(totals[c.id] for c in categories), sum(totals.values())])
    lines = ["", "Deliveries per day", *_table(table[0], table[1:])]
    return lines, totals


def _per_category_section(
    *, categories: Sequence[NotificationCategory], totals: Counter[str]
) -> list[str]:
    grand_total = sum(totals.values()) or 1
    return [
        "",
        "Per category",
        *_table(
            ["category", "delivered", "share"],
            [
                [category.id, totals[category.id], f"{totals[category.id] / grand_total:.1%}"]
                for category in categories
            ],
        ),
    ]


def _per_hour_section(*, window: _Window, baseline: Replay) -> list[str]:
    hours = per_hour(baseline.delivered, tz=window.tz)
    peak = max(hours) if hours else 0
    return [
        "",
        f"Per hour ({window.label})",
        *_table(
            ["hour", "count", ""],
            [[f"{hour:02d}", count, _bar(count, peak)] for hour, count in enumerate(hours)],
        ),
    ]


def _suppression_section(
    *, categories: Sequence[NotificationCategory], baseline: Replay
) -> list[str]:
    rows: list[list[object]] = []
    for category in categories:
        delivered = [c for c in baseline.delivered if c.category == category.id]
        keys = {c.dedupe_key for c in delivered if c.dedupe_key is not None}
        rows.append(
            [
                category.id,
                len(delivered) + baseline.suppressed(category.id),
                len(delivered),
                baseline.suppressed(category.id),
                len(delivered) - len(keys),
            ]
        )
    return [
        "",
        "Candidates and suppression",
        *_table(["category", "candidates", "delivered", "suppressed", "repeat deliveries"], rows),
    ]


def _simulation_section(
    *, variants: Sequence[tuple[str, Replay]], baseline: Replay, days: int
) -> list[str]:
    base_total = len(baseline.delivered)
    rows = [
        [
            name,
            len(result.delivered),
            f"{len(result.delivered) - base_total:+d}",
            f"{len(result.delivered) / days:.1f}",
        ]
        for name, result in variants
    ]
    return [
        "",
        "Simulations (one flipped input at a time, against the configured baseline)",
        *_table(["variant", "delivered", "delta", "per day"], rows),
    ]


def _source_lines(categories: Sequence[NotificationCategory]) -> list[str]:
    entries = [_SOURCES[category.id] for category in categories]
    return [f"sources   {entries[0]}", *(f"          {entry}" for entry in entries[1:])]


def render(
    *,
    window: _Window,
    categories: Sequence[NotificationCategory],
    switches: Mapping[str, bool],
    baseline: Replay,
    variants: Sequence[tuple[str, Replay]],
) -> list[str]:
    stamp = "%Y-%m-%d %H:%M"
    per_day_lines, totals = _per_day_section(
        window=window, categories=categories, baseline=baseline
    )
    return [
        f"Notification audit — {window.days} day{'' if window.days == 1 else 's'} ending "
        f"{window.end.astimezone(window.tz):{stamp}} {window.label}",
        f"window    {window.start.astimezone(window.tz):{stamp}}"
        f" .. {window.end.astimezone(window.tz):{stamp}}",
        *_source_lines(categories),
        "switches  "
        + "  ".join(f"{name}={_on_off(value=value)}" for name, value in switches.items()),
        *per_day_lines,
        *_per_category_section(categories=categories, totals=totals),
        *_per_hour_section(window=window, baseline=baseline),
        *_suppression_section(categories=categories, baseline=baseline),
        *_simulation_section(variants=variants, baseline=baseline, days=window.days),
    ]


def _on_off(*, value: bool) -> str:
    return "on" if value else "off"


def cmd_notices_audit(
    *,
    database_factory: DatabaseFactory,
    days: int,
    category: str | None,
    tz_name: str | None,
    now: datetime | None,
    simulate: Sequence[str],
) -> int:
    try:
        window = _window(days=days, tz_name=tz_name, now=now)
        selected, simulations = _selection(category=category, simulate=simulate)
    except ValueError as exc:
        print(f"meiva: {exc}", file=sys.stderr)
        return 2

    catalog = core_catalog()
    declared = [catalog[name] for name in selected]
    overrides = read_overrides_sync(database_factory().pool())
    candidates = load_candidates(
        database_factory,
        start=window.start,
        end=window.end,
        categories=selected,
        requestion=True,
    )
    baseline = replay(candidates, categories=catalog, switches=overrides)
    lines = render(
        window=window,
        categories=declared,
        switches=effective_switches(declared, overrides),
        baseline=baseline,
        variants=_variants(
            simulations=simulations,
            selected=selected,
            window=window,
            database_factory=database_factory,
            catalog=catalog,
            overrides=overrides,
            candidates=candidates,
            baseline=baseline,
        ),
    )
    print("\n".join(lines))
    return 0

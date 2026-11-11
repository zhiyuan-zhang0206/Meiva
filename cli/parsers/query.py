# meiva:note
# `meiva events`, `meiva metrics`, `meiva stats` and `meiva tasks` — read-only queries over the cluster's records, as
# thin clients of the gateway's read routes. Handlers lazy-import their `cmd_*` implementation so building the
# parser never loads Settings.

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import get_args

from base.events.contract import EventGroupBy
from base.host.boundary.options import options


@dataclass(frozen=True)
class _EventsFilterOptions:
    category: str | None
    name: str | None
    agent: int | None
    trace: str | None
    machine: str | None
    level: str | None
    tier: str | None
    since: str | None
    until: str | None
    hours: float | None
    limit: int | None
    json: bool


@dataclass(frozen=True)
class _EventsLsOptions(_EventsFilterOptions):
    offset: int | None
    total: bool


@dataclass(frozen=True)
class _EventsCountOptions(_EventsFilterOptions):
    by: EventGroupBy


def _h_events_ls(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.management.query.events import EventsFilter, cmd_events_ls
    from cli.database.runtime import runtime_of

    opts = options(args, _EventsLsOptions)
    flt = EventsFilter(
        category=opts.category,
        name=opts.name,
        agent_id=opts.agent,
        trace_id=opts.trace,
        machine=opts.machine,
        level=opts.level,
        tier=opts.tier,
        since=opts.since,
        until=opts.until,
        hours=opts.hours,
        limit=opts.limit,
        offset=opts.offset,
        with_total=opts.total,
    )
    return cmd_events_ls(gateway_access_from_boot(runtime_of(args).config), flt, as_json=opts.json)


def _h_events_count(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.management.query.events import EventsFilter, cmd_events_count
    from cli.database.runtime import runtime_of

    opts = options(args, _EventsCountOptions)
    flt = EventsFilter(
        category=opts.category,
        name=opts.name,
        agent_id=opts.agent,
        trace_id=opts.trace,
        machine=opts.machine,
        level=opts.level,
        tier=opts.tier,
        since=opts.since,
        until=opts.until,
        hours=opts.hours,
        limit=opts.limit,
    )
    return cmd_events_count(
        gateway_access_from_boot(runtime_of(args).config), flt, by=opts.by, as_json=opts.json
    )


def _add_event_filters(parser: argparse.ArgumentParser) -> None:
    # meiva:note
    # The filters `GET /api/events` and `GET /api/events/counts` share. Nothing here has a
    # default of its own: an omitted window is the gateway's last 24 hours.
    parser.add_argument("--category", choices=("audit", "telemetry", "log"))
    parser.add_argument("--name", help="exact event name, e.g. send_message or llm_usage")
    parser.add_argument("--agent", type=int, help="events of this agent id")
    parser.add_argument("--trace", help="events sharing this trace id")
    parser.add_argument("--machine", help="events recorded on this machine")
    parser.add_argument("--level", help="exact level: debug, info, warning, error or critical")
    parser.add_argument(
        "--tier", help="comma-separated tiers: business, anomaly, observation, noise"
    )
    parser.add_argument(
        "--from",
        dest="since",
        help="window start, ISO-8601 with an offset (e.g. 2026-11-12T00:00:00Z)",
    )
    parser.add_argument("--to", dest="until", help="window end, ISO-8601 with an offset")
    parser.add_argument(
        "--hours", type=float, help="the last N hours (instead of --from); default: last 24"
    )
    parser.add_argument("--limit", type=int, help="rows (ls) or groups (count), 1..1000")
    parser.add_argument("--json", action="store_true", help="print the gateway response as JSON")


def add_events_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # meiva:note
    # `meiva events ls|count` reads the unified event stream through the gateway.
    events_p = sub.add_parser("events", help="read the audit, telemetry and log event stream")
    events_sub = events_p.add_subparsers(dest="events_cmd", required=True)
    ls_p = events_sub.add_parser("ls", help="newest events matching the filters")
    _add_event_filters(ls_p)
    ls_p.add_argument("--offset", type=int, help="skip this many newer events")
    ls_p.add_argument(
        "--total", action="store_true", help="also count every matching event (one extra query)"
    )
    ls_p.set_defaults(func=_h_events_ls)
    count_p = events_sub.add_parser(
        "count", help="count the matching events per event name, machine, level, agent or category"
    )
    count_p.add_argument("--by", choices=get_args(EventGroupBy), required=True)
    _add_event_filters(count_p)
    count_p.set_defaults(func=_h_events_count)


@dataclass(frozen=True)
class _MetricsAgentsOptions:
    days: int | None
    since_compact: bool
    json: bool


@dataclass(frozen=True)
class _StatsOptions:
    hours: int | None
    json: bool


def _h_metrics_agents(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.management.query.metrics import cmd_metrics_agents
    from cli.database.runtime import runtime_of

    opts = options(args, _MetricsAgentsOptions)
    return cmd_metrics_agents(
        gateway_access_from_boot(runtime_of(args).config),
        days=opts.days,
        since_compact=opts.since_compact,
        as_json=opts.json,
    )


def _h_stats(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.management.query.metrics import cmd_stats
    from cli.database.runtime import runtime_of

    opts = options(args, _StatsOptions)
    return cmd_stats(
        gateway_access_from_boot(runtime_of(args).config), hours=opts.hours, as_json=opts.json
    )


def add_metrics_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # meiva:note
    # `meiva metrics agents` reads the per-agent cost, token and turn breakdown.
    metrics_p = sub.add_parser("metrics", help="per-agent cost, token and turn breakdown")
    metrics_sub = metrics_p.add_subparsers(dest="metrics_cmd", required=True)
    agents_p = metrics_sub.add_parser(
        "agents", help="one row per agent over the last N days, the costliest first"
    )
    agents_p.add_argument("--days", type=int, help="window in days, 1..30; default: the gateway's")
    agents_p.add_argument(
        "--since-compact",
        action="store_true",
        help="count each agent's events from its latest compaction on",
    )
    agents_p.add_argument("--json", action="store_true", help="print the gateway response as JSON")
    agents_p.set_defaults(func=_h_metrics_agents)


def add_stats_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # meiva:note
    # `meiva stats` reads the fleet totals of the sidebar Status popover and the plugin cards.
    stats_p = sub.add_parser(
        "stats", help="fleet cost, tokens and alert totals, and plugin status cards (balances)"
    )
    stats_p.add_argument(
        "--hours",
        type=int,
        help="window: 0 (last 5 minutes), 1, 6, 24, 72, 168 or 720 hours; default: the gateway's 24",
    )
    stats_p.add_argument("--json", action="store_true", help="print the gateway response as JSON")
    stats_p.set_defaults(func=_h_stats)


@dataclass(frozen=True)
class _TasksLsOptions:
    owner: int | None
    status: str | None
    window: str | None
    json: bool


@dataclass(frozen=True)
class _TasksGetOptions:
    task_id: int
    json: bool


def _h_tasks_ls(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.management.query.tasks import TasksFilter, cmd_tasks_ls
    from cli.database.runtime import runtime_of

    opts = options(args, _TasksLsOptions)
    flt = TasksFilter(owner=opts.owner, status=opts.status, window=opts.window)
    return cmd_tasks_ls(gateway_access_from_boot(runtime_of(args).config), flt, as_json=opts.json)


def _h_tasks_get(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.management.query.tasks import cmd_tasks_get
    from cli.database.runtime import runtime_of

    opts = options(args, _TasksGetOptions)
    return cmd_tasks_get(
        gateway_access_from_boot(runtime_of(args).config), opts.task_id, as_json=opts.json
    )


def add_tasks_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # meiva:note
    # `meiva tasks ls|get` reads the task registry through the gateway.
    tasks_p = sub.add_parser("tasks", help="read the task registry")
    tasks_sub = tasks_p.add_subparsers(dest="tasks_cmd", required=True)
    ls_p = tasks_sub.add_parser(
        "ls", help="tasks matching a filter (at least one of --owner, --status, --window)"
    )
    ls_p.add_argument("--owner", type=int, help="tasks owned by this agent id")
    ls_p.add_argument("--status", choices=("in_progress", "done", "cancelled"))
    ls_p.add_argument(
        "--window",
        choices=("24h", "7d", "30d", "all"),
        help="tasks active in the window (in-progress tasks always count); 'all' is everything",
    )
    ls_p.add_argument("--json", action="store_true", help="print the gateway response as JSON")
    ls_p.set_defaults(func=_h_tasks_ls)
    get_p = tasks_sub.add_parser("get", help="one task in full: fields, reminder state, results")
    get_p.add_argument("task_id", type=int)
    get_p.add_argument("--json", action="store_true", help="print the gateway response as JSON")
    get_p.set_defaults(func=_h_tasks_get)

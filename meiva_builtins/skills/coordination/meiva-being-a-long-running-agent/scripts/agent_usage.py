"""Report recorded LLM usage for chosen agents, a time window, and birth lineage.

Run on Meiva's Python with cluster database access. Optional polling sends a
one-shot budget reminder to explicitly selected peers; it never terminates them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_budget import (
    BudgetFacts,
    Finish,
    Notify,
    Sleep,
    budget_message,
    check_usd_limit,
    next_step,
    parse_time,
    positive_id,
)

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.boundary.fields import (
    int_field,
    optional_float_field,
    optional_list_field,
    optional_object_field,
    optional_str_field,
    str_field,
)
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.options import options
from base.host.boundary.untyped import object_of
from base.host.exit_codes import hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import process_name
from base.telemetry.metrics.usage import Lineage, usage_report


def notify_agents(recipients: Sequence[int], message: str) -> None:
    """Notify only named peers, through the existing core communication API."""
    import meiva.agents

    for agent_id in sorted(set(recipients)):
        meiva.agents.send_message(agent_id, message)


# meiva:note
# The flags this CLI reads, as real types: `options` checks every field against the annotation
# here, so nothing downstream of `parse_args` holds an `argparse.Namespace` attribute's `Any`.
# The two appended flags are `Sequence`, not `list`: `frozen` here means this carrier is never
# rewritten, not that it is hashable — neither field is ever hashed, and argparse hands over the
# list it built, which `options` then checks element by element.
@dataclass(frozen=True)
class UsageOptions:
    agent_id: Sequence[int]
    lineage: Lineage
    lifetime: bool
    start: datetime | None
    end: datetime | None
    usd_limit: float | None
    notify_agent: Sequence[int]
    poll_seconds: float | None


def parse_args(argv: Sequence[str]) -> UsageOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", type=positive_id, action="append", required=True)
    parser.add_argument("--lineage", choices=("self", "spawn", "fork", "all"), default="self")
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--start", type=parse_time)
    window.add_argument(
        "--lifetime", action="store_true", help="Folded lifetime ledger plus current event tail"
    )
    parser.add_argument("--end", type=parse_time, help="Default: current time on each poll")
    parser.add_argument("--usd-limit", type=float)
    parser.add_argument("--notify-agent", type=positive_id, action="append", default=[])
    parser.add_argument(
        "--poll-seconds",
        type=float,
        help="Poll until breach; bound lifetime with a watcher timeout",
    )
    parsed = options(parser.parse_args(list(argv)), UsageOptions)
    validate_args(parsed, parser)
    return parsed


def validate_args(parsed: UsageOptions, parser: argparse.ArgumentParser) -> None:
    if parsed.poll_seconds is not None:
        if not math.isfinite(parsed.poll_seconds) or parsed.poll_seconds <= 0:
            parser.error("--poll-seconds must be positive and finite")
        if parsed.end or not parsed.notify_agent or parsed.usd_limit is None:
            parser.error("polling requires a moving end, a limit, and notification recipients")
    if parsed.notify_agent and parsed.usd_limit is None:
        parser.error("notification requires --usd-limit")
    if parsed.lifetime and parsed.end:
        parser.error("--lifetime cannot end at a historical timestamp")
    # Validate thresholds before database access, even on an empty report.
    check_usd_limit(parsed.usd_limit)


# meiva:note
# What a reminder states about a usage report, read from the report's totals and agent rows.
# `usage_report` answers `dict[str, Any]`; every field read here goes through `fields`, so a
# malformed report raises a named `TypeError` instead of leaking an unchecked value into the
# reminder.
def facts_of(report: object) -> BudgetFacts:
    if not is_object_dict(report):
        raise TypeError("the usage report is not an object")
    totals = optional_object_field(report, "totals")
    if totals is None:
        raise TypeError("the usage report carries no totals object")
    recorded_cost_usd = optional_float_field(totals, "recorded_cost_usd")
    if recorded_cost_usd is None:
        raise TypeError("the usage report totals carry no recorded_cost_usd")
    return BudgetFacts(
        recorded_cost_usd=recorded_cost_usd,
        unpriced_calls=int_field(totals, "unpriced_calls"),
        agent_ids=tuple(_agent_id(row) for row in optional_list_field(report, "agents") or []),
        lineage=str_field(report, "lineage"),
        start=optional_str_field(report, "start"),
        end=str_field(report, "end"),
    )


# meiva:note
# The `agent_id` of one agent row of a usage report; a row that is not an object is malformed.
def _agent_id(row: object) -> int:
    if not is_object_dict(row):
        raise TypeError("the usage report lists an agent row that is not an object")
    return int_field(row, "agent_id")


# meiva:note
# Read the usage report each round inside one read-only snapshot, print it as JSON, and let `next_step` decide: a reminder goes to the named peers once and ends the run, a polling run waits and reads again. The window's end is the clock at each round unless `--end` fixed it.
def main(argv: Sequence[str], *, database: Database) -> None:
    parsed = parse_args(argv)
    while True:
        with database.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # meiva:note
            # `usage_report` answers `dict[str, Any]`; `object_of` keeps that `Any` out of this
            # module and `facts_of` reads the fields it uses through the boundary helpers.
            report = object_of(
                usage_report,
                conn,
                roots=parsed.agent_id,
                lineage=parsed.lineage,
                start=parsed.start,
                end=parsed.end or datetime.now(UTC),
            )
        message = budget_message(facts_of(report), usd_limit=parsed.usd_limit)
        print(
            json.dumps(report, sort_keys=True), flush=True
        )  # script output, not framework logging
        step = next_step(message, parsed.poll_seconds)
        if isinstance(step, Notify):
            notify_agents(parsed.notify_agent, step.message)
            return
        if isinstance(step, Finish):
            return
        if isinstance(step, Sleep):
            time.sleep(step.seconds)
        else:
            assert_never(step)


# meiva:note
# The composition root of the CLI: one captured entry gate and the database handle built on it, kept for every poll.
def process_database() -> Database:
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process=process_name(), end_process=hard_exit)
    return Database.from_settings(gate=gate)


if __name__ == "__main__":
    parse_args(sys.argv[1:])
    main(sys.argv[1:], database=process_database())

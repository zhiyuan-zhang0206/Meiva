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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, assert_never

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
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import process_name
from base.telemetry.metrics.usage import usage_report


def notify_agents(recipients: Sequence[int], message: str) -> None:
    """Notify only named peers, through the existing core communication API."""
    import meiva.agents

    for agent_id in sorted(set(recipients)):
        meiva.agents.send_message(agent_id, message)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
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
    args = parser.parse_args(list(argv))
    validate_args(args, parser)
    return args


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.poll_seconds is not None:
        if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
            parser.error("--poll-seconds must be positive and finite")
        if args.end or not args.notify_agent or args.usd_limit is None:
            parser.error("polling requires a moving end, a limit, and notification recipients")
    if args.notify_agent and args.usd_limit is None:
        parser.error("notification requires --usd-limit")
    if args.lifetime and args.end:
        parser.error("--lifetime cannot end at a historical timestamp")
    # Validate thresholds before database access, even on an empty report.
    check_usd_limit(args.usd_limit)


# meiva:note
# What a reminder states about a usage report, read from the report's totals and agent rows.
def facts_of(report: dict[str, Any]) -> BudgetFacts:
    totals = report["totals"]
    return BudgetFacts(
        recorded_cost_usd=totals["recorded_cost_usd"],
        unpriced_calls=totals["unpriced_calls"],
        agent_ids=tuple(agent["agent_id"] for agent in report["agents"]),
        lineage=report["lineage"],
        start=report["start"],
        end=report["end"],
    )


# meiva:note
# Read the usage report each round inside one read-only snapshot, print it as JSON, and let `next_step` decide: a reminder goes to the named peers once and ends the run, a polling run waits and reads again. The window's end is the clock at each round unless `--end` fixed it.
def main(argv: Sequence[str], *, database: Database) -> None:
    args = parse_args(argv)
    while True:
        with database.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            report = usage_report(
                conn,
                roots=args.agent_id,
                lineage=args.lineage,
                start=args.start,
                end=args.end or datetime.now(UTC),
            )
        message = budget_message(facts_of(report), usd_limit=args.usd_limit)
        print(
            json.dumps(report, sort_keys=True), flush=True
        )  # script output, not framework logging
        step = next_step(message, args.poll_seconds)
        if isinstance(step, Notify):
            notify_agents(args.notify_agent, step.message)
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
    gate = ProcessDbGate(version=version.get, process=process_name())
    return Database.from_settings(gate=gate)


if __name__ == "__main__":
    parse_args(sys.argv[1:])
    main(sys.argv[1:], database=process_database())

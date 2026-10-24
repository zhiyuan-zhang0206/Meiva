# meiva:note
# `window_records.py` turns the rows one collection read into the dataset's records, as values only: events grouped per agent, the agents that become records, and each record with the code-body counters added. `collect.py` reads the events, rows and transcripts and hands them in. An agent with no lifecycle row is no real run and is skipped; one whose role label marks a benchmark or probe spawn is excluded (`test_ids` is empty when the caller asked for them), and `Selection` reports both drops so the daily sentinel can tell a TEST-only window from a broken data source.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
from __future__ import annotations

import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from record import AgentMeta, Event, Inbound, RunInputs, build_record

_BUILTIN_HELP_CALL_RE = re.compile(r"(?<![a-zA-Z0-9_.])help\s*\(")
_BUILTIN_HELP_ON_MEIVA_RE = re.compile(r"(?<![a-zA-Z0-9_.])help\s*\(\s*meiva\.")
_SUBPROCESS_CALL_RE = re.compile(r"subprocess\.(run|Popen|check_output|check_call|call)\b")


# meiva:note
# Group EventRow dicts into {agent_id: [(event_name, attributes)]}; service-level rows (agent_id None) are dropped.
def group_by_agent(rows: Sequence[Mapping[str, object]]) -> dict[int, list[Event]]:
    out: dict[int, list[Event]] = defaultdict(list)
    for row in rows:
        agent_id = row.get("agent_id")
        if agent_id is None:
            continue
        attributes = row.get("attributes")
        out[cast(int, agent_id)].append(
            (cast(str, row["event_name"]), cast(Mapping[str, object], attributes or {}))
        )
    return out


def _builtin_help_counts(events: Sequence[Event]) -> tuple[int, int]:
    calls = 0
    on_meiva = 0
    for event, payload in events:
        if event != "code":
            continue
        body = str(payload.get("body", ""))
        calls += len(_BUILTIN_HELP_CALL_RE.findall(body))
        on_meiva += len(_BUILTIN_HELP_ON_MEIVA_RE.findall(body))
    return calls, on_meiva


# meiva:note
# Count raw subprocess calls across executed code blocks with a regex. The count includes matching names in comments, docstrings and string literals, and it does not count ``asyncio.create_subprocess_*`` or ``subprocess.getoutput``.
def subprocess_call_count(events: Sequence[Event]) -> int:
    return sum(
        len(_SUBPROCESS_CALL_RE.findall(str(payload.get("body", ""))))
        for event, payload in events
        if event == "code"
    )


# meiva:note
# The record of one run with the code-body counters added: builtin `help()` calls, the subset aimed at `meiva.*` objects, and raw subprocess calls.
def annotated_record(run: RunInputs) -> dict[str, object]:
    record = build_record(run)
    record["builtin_help_calls"], record["builtin_help_on_meiva"] = _builtin_help_counts(run.events)
    record["subprocess_calls"] = subprocess_call_count(run.events)
    return record


class Selection(NamedTuple):
    kept: list[int]
    excluded_test: int
    skipped_meta: int


# meiva:note
# Which of the window's agents become records, in id order. The lifecycle check comes first, so an agent that is both unknown to `agents_meta` and a test spawn counts as skipped.
def select_agents(ids: Sequence[int], meta_ids: Set[int], test_ids: Set[int]) -> Selection:
    kept: list[int] = []
    excluded_test = 0
    skipped_meta = 0
    for agent_id in ids:
        if agent_id not in meta_ids:
            skipped_meta += 1
        elif agent_id in test_ids:
            excluded_test += 1
        else:
            kept.append(agent_id)
    return Selection(kept, excluded_test, skipped_meta)


# meiva:note
# The window's rows per agent id, read by `collect.py`: telemetry events, audit events, inbounds, lifecycle rows, task-receipt counts, transcripts and the skills each agent loaded.
@dataclass(frozen=True)
class WindowRows:
    events: Mapping[int, list[Event]]
    logs: Mapping[int, list[Event]]
    inbounds: Mapping[int, list[Inbound]]
    meta: Mapping[int, AgentMeta]
    receipts: Mapping[int, dict[str, int]]
    transcripts: Mapping[int, list[dict[str, str]]]
    skills: Mapping[int, list[str]]


# meiva:note
# One annotated record per selected agent, in the order given.
def window_records(selected: Sequence[int], week: str, rows: WindowRows) -> list[dict[str, object]]:
    return [
        annotated_record(
            RunInputs(
                agent_id=agent_id,
                week=week,
                events=rows.events.get(agent_id, []),
                log_events=rows.logs.get(agent_id, []),
                inbounds=rows.inbounds.get(agent_id, []),
                meta=rows.meta[agent_id],
                transcript=rows.transcripts[agent_id],
                skills_touched=rows.skills[agent_id],
                task_receipts=rows.receipts.get(agent_id),
            )
        )
        for agent_id in selected
    ]

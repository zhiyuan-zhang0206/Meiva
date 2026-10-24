#!/usr/bin/env python3
"""Harvest the past week's real agent runs into a trace dataset file.

One JSON record per agent that was active in the window, written as JSONL to
`$MEIVA_HOME/self_evolution/dataset/<week>.jsonl`. Each record is assembled from
existing tables only (no new schema): the unified `events` stream —
`category=telemetry/log` carries token / turn / exec signals, `category=audit`
carries compaction and the future
`skill_invoked` hard signal), `inbound_messages` (the user's task prompt and
any follow-up corrections), `agents_meta` (spawner, terminal status, final
message), and the stored checkpoint (the full transcript reconstructed across
compaction boundaries retained by Task #1125; only duplicate system prompts
are removed, fixing the 2026-08-10 truncation without hiding lived messages).

The dataset is the durable asset this whole skill iterates on: real tasks,
real traces, real outcomes. It lives under `$MEIVA_HOME` (per-deployment,
private) so the skill package itself stays publishable.

Usage:

    .venv/bin/python skills/meiva-self-evolution/scripts/collect.py --days 7
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, assert_never

import httpx
import psycopg
from pydantic import ValidationError

from base.db import Database

# `record` is a sibling in this skill's own scripts/ dir, not an importable
# package (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path) —
# restore it for the sibling import. Structure Rule 6 recognizes this exact
# __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from events_window import (
    MIN_SLICE,
    PAGE,
    Split,
    Take,
    Window,
    http_failure_detail,
    merge_slices,
    page_params,
    resolve_window,
    slice_step,
    week_label,
)
from record import HUMAN_SOURCES, AgentMeta, Event, Inbound, LeakPaths, RunInputs, transcript_rows
from window_records import (
    WindowRows,
    annotated_record,
    group_by_agent,
    select_agents,
    window_records,
)

from base.agents.history.checkpoint import CheckpointReadError, load_checkpoint_messages_full
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.host.net.resilience import ExponentialBackoff, Policy, http_classifier, retry
from base.telemetry.audit_events import SkillInvokedPayload

_HTTP_TIMEOUT_S = 120.0
_EVENTS_PATH = "/api/events"


# ─────── /api/events retry budget + failure diagnostics ─────────────────────
# Task #3126 (2026-09-12): a transient blip at the daily scan must not cost
# the day's dataset. Only 502/503/504 and transport errors are retryable: a
# forwarding hop that failed (2026-09-12: an env-configured local proxy
# answered 502 for a healthy gateway) or the gateway shedding (503/504).
# Narrower than the shared 429/5xx default on purpose — any other status is
# structural for this once-a-day read: fail fast so the scan reports the
# real error instead of spending the budget on it.
_EVENTS_CLASSIFY = http_classifier.with_(
    transient={502, 503, 504},
    permanent={429, 500},  # the shared default's other transient statuses
)


def _exhausted_fetch_error(exc: Exception) -> RuntimeError:
    """The error raised once the retry budget is spent.

    Carries the evidence that identifies the responder without re-running
    the scan: for an HTTP failure the status line plus bounded header/body
    excerpts — a local forward proxy's 502 and the gateway's own 503 are
    otherwise indistinguishable — and for a transport failure its own name
    and message.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        detail = http_failure_detail(
            response.status_code, response.reason_phrase, response.headers, response.text
        )
    else:
        detail = f"{type(exc).__name__}: {exc}"
    attempts = _EVENTS_RETRY_POLICY.max_attempts
    return RuntimeError(f"/api/events read failed after {attempts} attempts: {detail}")


def _convert_retry_exhausted(exc: Exception) -> None:
    """on_final_failure hook: convert a spent budget, pass a fail-fast through.

    The retry executor calls this on every final failure; a retryable class
    reaches it only with the budget spent (otherwise it would have been
    retried), while a permanent class failed fast on the first attempt and
    re-raises untouched.
    """
    if _EVENTS_CLASSIFY(exc):
        raise _exhausted_fetch_error(exc) from exc


# Five attempts, sleeps 5/10/20/40 s — a ~75 s total wait budget (task
# #3126: 60-90 s). Jitter is off (a single scheduled runner, nobody to
# de-phase) and Retry-After is not honored: the budget is exactly this
# schedule, so the worst case stays known.
_EVENTS_RETRY_POLICY = Policy(
    max_attempts=5,
    backoff=ExponentialBackoff(base=5.0, factor=2.0, cap=40.0),
    classify=_EVENTS_CLASSIFY,
    jitter="none",
    respect_retry_after=False,
    on_final_failure=_convert_retry_exhausted,
)


# ─────── Loki-backed event fetches (paged via the gateway /api/events) ───────


def _events_page(
    client: httpx.Client, url: str, headers: dict[str, str], params: dict[str, str | int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One /api/events page (offset always 0). Returns (items, meta).

    Runs on `_EVENTS_RETRY_POLICY`: a transient failure (502/503/504 or a
    transport error) is retried inside the wait budget; a spent budget
    raises the diagnostics-carrying error, any other status fails fast.
    """

    def fetch() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        resp = client.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("items", []), payload.get("meta", {})

    return retry(_EVENTS_RETRY_POLICY)(fetch)


# meiva:note
# All rows for (category, window) via /api/events, oldest-first. The slicing, bisection and deduplication are `events_window`'s plan; this is the shell that reads one page per slice. It raises on persistent HTTP errors, because a partial window must not silently become a partial dataset, and a spent retry budget raises with the responder's status, headers and body snippet. The gateway URL and bearer come from `gateway`, so an unset URL or a process without its machine token fails here, before any request.
# The client is built with `trust_env=False` (2026-09-12): cluster-internal reads must not be routed through an env-configured proxy. httpx otherwise honors HTTP(S)_PROXY / NO_PROXY, and a no_proxy form it does not match hands these requests to a local forward proxy that cannot reach the gateway, which answered 502 while the gateway was healthy. Same pattern as ops/cluster/rpc.py and cli/commands/observability/trace.py, for the same reason.
def fetch_events_window(
    category: str,
    window: Window,
    *,
    gateway: GatewayAccess,
    agent_id: int | None = None,
) -> list[Mapping[str, object]]:
    url = gateway_api_base(gateway.gateway_url) + _EVENTS_PATH
    headers = gateway_auth_headers(gateway)
    slices: list[list[Mapping[str, object]]] = []

    with httpx.Client(timeout=_HTTP_TIMEOUT_S, trust_env=False) as client:

        def read(start: datetime, end: datetime) -> None:
            if start >= end:
                return
            batch, meta = _events_page(
                client, url, headers, page_params(category, start, end, agent_id)
            )
            step = slice_step(batch, meta, start, end)
            if isinstance(step, Take):
                if step.truncated:
                    print(
                        f"warning: {category} slice [{start}, {end}] has more than "
                        f"{PAGE} rows in under {MIN_SLICE} — tail may be truncated"
                    )
                slices.append(step.rows)
            elif isinstance(step, Split):
                read(start, step.mid)
                read(step.mid, end)
            else:
                assert_never(step)

        read(window.start, window.end)

    return merge_slices(slices)


# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
def _inbounds_by_agent(
    cur: psycopg.Cursor, agent_ids: list[int], window_from: datetime, window_to: datetime
) -> dict[int, list[Inbound]]:
    cur.execute(
        "SELECT DISTINCT ON (inbound.agent_id, inbound.source) "
        "inbound.id, inbound.agent_id, inbound.source, inbound.content, "
        "inbound.claimed_at IS NOT NULL AS claimed, inbound.created_at "
        "FROM inbound_messages inbound "
        "LEFT JOIN agents_meta meta ON meta.id = inbound.agent_id "
        "WHERE inbound.agent_id = ANY(%s) AND inbound.kind = 'chat' "
        "AND (inbound.source = meta.spawner OR inbound.source = ANY(%s) "
        "OR inbound.source LIKE 'schedule:%%') "
        "ORDER BY inbound.agent_id, inbound.source, inbound.created_at",
        [agent_ids, sorted(HUMAN_SOURCES)],
    )
    task_rows = cur.fetchall()
    task_ids = {row[0] for row in task_rows}
    cur.execute(
        "WITH window_inbounds AS ("
        "SELECT id, agent_id, source, content, claimed_at IS NOT NULL AS claimed, created_at "
        "FROM inbound_messages "
        "WHERE agent_id = ANY(%s) AND kind = 'chat' "
        "AND (source = ANY(%s) OR source LIKE 'agent:%%') "
        "AND created_at >= %s AND created_at < %s"
        "), broadcasts AS ("
        "SELECT source, content FROM window_inbounds "
        "GROUP BY source, content HAVING count(DISTINCT agent_id) > 1"
        ") "
        "SELECT inbound.id, inbound.agent_id, inbound.source, inbound.content, "
        "inbound.claimed, inbound.created_at, broadcasts.source IS NOT NULL AS is_broadcast "
        "FROM window_inbounds inbound "
        "LEFT JOIN broadcasts ON broadcasts.source = inbound.source "
        "AND broadcasts.content = inbound.content "
        "ORDER BY inbound.agent_id, inbound.created_at",
        [agent_ids, sorted(HUMAN_SOURCES), window_from, window_to],
    )
    rows: dict[int, list[tuple[datetime, Inbound]]] = defaultdict(list)
    for _msg_id, agent_id, source, content, claimed, created_at in task_rows:
        rows[agent_id].append(
            (
                created_at,
                {
                    "source": source,
                    "content": content,
                    "is_broadcast": False,
                    "claimed": claimed,
                },
            )
        )
    for msg_id, agent_id, source, content, claimed, created_at, is_broadcast in cur.fetchall():
        if msg_id in task_ids:
            continue
        rows[agent_id].append(
            (
                created_at,
                {
                    "source": source,
                    "content": content,
                    "is_broadcast": is_broadcast,
                    "claimed": claimed,
                },
            )
        )
    return {
        agent_id: [message for _, message in sorted(entries, key=lambda entry: entry[0])]
        for agent_id, entries in rows.items()
    }


def _task_receipts_by_agent(cur: psycopg.Cursor, ids: list[int]) -> dict[int, dict[str, int]]:
    cur.execute(
        "WITH status_receipts AS ("
        "SELECT actor_agent_id, task_id, request->>'status' AS declared, "
        "lag(request->>'status') OVER ("
        "PARTITION BY actor_agent_id, task_id ORDER BY accepted_at, operation_key"
        ") AS previous "
        "FROM task_update_receipts "
        "WHERE actor_agent_id = ANY(%s) AND request->>'status' IS NOT NULL"
        "), state_changes AS ("
        "SELECT actor_agent_id, count(*) AS changes FROM status_receipts "
        "WHERE declared IS DISTINCT FROM COALESCE(previous, 'in_progress') "
        "GROUP BY actor_agent_id"
        ") "
        "SELECT r.actor_agent_id, "
        "count(*) FILTER ("
        "WHERE r.request->>'note' IS NOT NULL AND r.request->>'status' IS NULL"
        "), "
        "count(*) FILTER (WHERE r.request->>'status' IS NOT NULL), "
        "count(*) FILTER (WHERE r.request->>'results' IS NOT NULL), "
        "COALESCE(max(c.changes), 0) "
        "FROM task_update_receipts r "
        "LEFT JOIN state_changes c ON c.actor_agent_id = r.actor_agent_id "
        "WHERE r.actor_agent_id = ANY(%s) "
        "GROUP BY r.actor_agent_id",
        [ids, ids],
    )
    return {
        row[0]: {"note": row[1], "status": row[2], "results": row[3], "state_changes": row[4]}
        for row in cur.fetchall()
    }


def _meta_by_agent(cur: psycopg.Cursor, ids: list[int]) -> dict[int, AgentMeta]:
    cur.execute(
        "SELECT id, spawner, status, last_message_text FROM agents_meta WHERE id = ANY(%s)",
        [ids],
    )
    return {r[0]: AgentMeta(r[1], r[2], r[3]) for r in cur.fetchall()}


def _test_label_ids(cur: psycopg.Cursor, ids: list[int]) -> set[int]:
    """Benchmark/probe agents and their direct workers, excluded from health data.

    A TEST- orchestrator's workers inherit its benchmark-only purpose even
    though their own labels describe the task they execute.
    """
    cur.execute(
        "SELECT child.id, child.label, parent.label "
        "FROM agents child "
        "LEFT JOIN agents_meta child_meta ON child_meta.id = child.id "
        "LEFT JOIN agents parent ON child_meta.spawner = 'agent:' || parent.id::text "
        "WHERE child.id = ANY(%s)",
        [ids],
    )
    return {
        row[0]
        for row in cur.fetchall()
        if (row[1] or "").startswith("TEST-") or (row[2] or "").startswith("TEST-")
    }


# ─────────────── driver ───────────────


# meiva:note
# The agent's full transcript rows, or none when its checkpoint cannot be read. An unreadable checkpoint leaves the transcript empty instead of failing the scan, and says so: the record then grades below `complete` and shows in the scan's coverage line.
def _transcript(agent_id: int, *, database: Database) -> list[dict[str, str]]:
    try:
        messages = load_checkpoint_messages_full(database, agent_id)
    except CheckpointReadError as exc:
        print(f"warning: agent {agent_id}: checkpoint unreadable, transcript left empty: {exc!r}")
        return []
    return transcript_rows(messages)


# meiva:note
# The skills an agent loaded, sorted: the audit rows named `skill_invoked` whose payload validates as `SkillInvokedPayload` with invocation depth `loaded`. A payload that does not validate is not evidence of a load and is skipped.
def loaded_skills(log_events: Sequence[Event]) -> list[str]:
    loaded: set[str] = set()
    for event_type, payload in log_events:
        if event_type != "skill_invoked":
            continue
        with contextlib.suppress(ValidationError):
            entry = SkillInvokedPayload.model_validate(payload)
            if entry.invocation_depth == "loaded":
                loaded.add(entry.skill)
    return sorted(loaded)


def _warn_skipped_plugin_rows(records: Sequence[Mapping[str, object]]) -> None:
    for record in records:
        skipped = record["plugins_activated_skipped"]
        if skipped:
            print(
                f"warning: agent {record['agent_id']}: skipped {skipped} "
                "malformed plugin_activation row(s)"
            )


# meiva:note
# One record per agent active in the window, plus the pre-record counts the daily sentinel needs. Returns (records, counts) where counts is {"seen": distinct window agents with events, "excluded_test": dropped by the TEST- filter, "skipped_meta": dropped for a missing lifecycle row}. The daily scan's empty-dataset sentinel (2026-08-14: 0 runs = data source outage) keys on pre-filter activity, so a TEST-only window, a healthy source whose every run the filter removed by design, stays distinguishable from a source that produced nothing (QA review of PR #698, 2026-08-29).
# Events come from Loki via the gateway /api/events endpoint; inbounds and lifecycle metadata come from PG. Category 'log' is not fetched: it carries only exec stdout payloads, which build_record never reads, and every agent that logs stdout also emits telemetry events, so agent discovery loses nothing. Agents whose role label carries the TEST- prefix (benchmark / probe spawns) are excluded by default so they never reach the health dataset or its labels; pass `include_test=True` to collect them (a measurement run that reads the same fields).
def collect_with_counts(
    window: Window,
    week: str,
    *,
    database: Database,
    gateway: GatewayAccess,
    include_test: bool = False,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    telemetry = fetch_events_window("telemetry", window, gateway=gateway)
    audit = fetch_events_window("audit", window, gateway=gateway)
    events = group_by_agent(telemetry)
    logs = group_by_agent(audit)
    ids = sorted(events)
    with database.connect() as conn, conn.cursor() as cur:
        inbounds = _inbounds_by_agent(cur, ids, window.start, window.end)
        meta = _meta_by_agent(cur, ids)
        receipts = _task_receipts_by_agent(cur, ids)
        test_ids: set[int] = set() if include_test else _test_label_ids(cur, ids)
    selection = select_agents(ids, meta.keys(), test_ids)
    rows = WindowRows(
        events=events,
        logs=logs,
        inbounds=inbounds,
        meta=meta,
        receipts=receipts,
        transcripts={
            agent_id: _transcript(agent_id, database=database) for agent_id in selection.kept
        },
        skills={agent_id: loaded_skills(logs.get(agent_id, [])) for agent_id in selection.kept},
    )
    records = window_records(selection.kept, week, rows)
    _warn_skipped_plugin_rows(records)
    counts = {
        "seen": len(ids),
        "excluded_test": selection.excluded_test,
        "skipped_meta": selection.skipped_meta,
    }
    return records, counts


def collect_one(
    agent_id: int,
    week: str,
    leak_paths: LeakPaths | None = None,
    *,
    database: Database,
    gateway: GatewayAccess,
    now: datetime,
) -> dict[str, object]:
    # meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
    window = Window(now - timedelta(days=7), now)
    telemetry = fetch_events_window("telemetry", window, gateway=gateway, agent_id=agent_id)
    audit = fetch_events_window("audit", window, gateway=gateway, agent_id=agent_id)
    events = group_by_agent([r for r in telemetry if r.get("agent_id") == agent_id]).get(
        agent_id, []
    )
    log_events = group_by_agent([r for r in audit if r.get("agent_id") == agent_id]).get(
        agent_id, []
    )
    with database.connect() as conn, conn.cursor() as cur:
        inbounds = _inbounds_by_agent(cur, [agent_id], window.start, window.end).get(agent_id, [])
        meta = _meta_by_agent(cur, [agent_id]).get(agent_id)
        receipts = _task_receipts_by_agent(cur, [agent_id]).get(agent_id)
    if meta is None:
        raise ValueError(f"no agents_meta row for agent {agent_id}")
    record = annotated_record(
        RunInputs(
            agent_id=agent_id,
            week=week,
            events=events,
            log_events=log_events,
            inbounds=inbounds,
            meta=meta,
            transcript=_transcript(agent_id, database=database),
            skills_touched=loaded_skills(log_events),
            leak_paths=leak_paths,
            task_receipts=receipts,
        )
    )
    _warn_skipped_plugin_rows([record])
    return record


def dataset_path(home: Path, week: str) -> Path:
    out_dir = home / "self_evolution" / "dataset"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{week}.jsonl"


# meiva:note
# Write the records to `path`, one JSON object per line.
def write_records(records: Sequence[Mapping[str, object]], path: Path) -> Path:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return path


def write_dataset(records: Sequence[Mapping[str, object]], home: Path, week: str) -> Path:
    """Write dataset to a dated JSONL file. Pass an explicit `--week` to
    regenerate a previous file in place; the default uses today's date."""
    return write_records(records, dataset_path(home, week))


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Harvest the past week's agent runs into a trace dataset (JSONL).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--days", type=int, default=7, help="window size in days (default 7)")
    p.add_argument(
        "--week", default=None, help="week label / output filename stem (default: this Monday)"
    )
    p.add_argument(
        "--from",
        dest="from_",
        default=None,
        help="window start, ISO-8601 with timezone (default: now - days) — use to backfill a missed day",
    )
    p.add_argument(
        "--to",
        default=None,
        help="window end, ISO-8601 with timezone (default: now)",
    )
    p.add_argument(
        "--include-test",
        action="store_true",
        help="include TEST- prefixed benchmark/probe spawns (measurement only; default excludes)",
    )
    return p.parse_args(list(argv))


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"window bounds must carry a timezone offset: {value!r}")
    return parsed.astimezone(UTC)


# meiva:note
# The process facts a collection run reads: its database, gateway access, the clock reading of its start and its home.
@dataclass(frozen=True)
class CollectEnv:
    database: Database
    gateway: GatewayAccess
    now: datetime
    home: Path


def main(argv: Sequence[str], env: CollectEnv) -> None:
    args = parse_args(argv)
    week = args.week or week_label(env.now)
    window = resolve_window(
        env.now,
        args.days,
        _parse_iso(args.from_) if args.from_ else None,
        _parse_iso(args.to) if args.to else None,
    )
    records, _counts = collect_with_counts(
        window, week, database=env.database, gateway=env.gateway, include_test=args.include_test
    )
    path = write_dataset(records, env.home, week)
    counts = Counter(r["label"] for r in records)
    stamp = env.now.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"[{stamp}] collected {len(records)} runs "
        f"(ok {counts['ok']} / fumbled {counts['fumbled']} / failed {counts['failed']}"
        f" / unknown {counts['unknown']}) "
        f"-> {path}"
    )


# meiva:note
# The composition root of the standalone run: this process's database, gateway access, clock and home.
def process_env() -> CollectEnv:
    from base.cluster.machine.ambient import gateway_access_from_settings
    from base.db.code_version_gate import ProcessDbGate
    from base.native_process.code_version import CodeVersion
    from base.native_process.loaded_commit import LoadedCommit
    from base.paths import meiva_home

    version = CodeVersion(LoadedCommit.capture())
    gate = ProcessDbGate(version=version.get, process="unknown")
    return CollectEnv(
        database=Database.from_settings(gate=gate),
        gateway=gateway_access_from_settings(),
        now=datetime.now(UTC),
        home=meiva_home(),
    )


if __name__ == "__main__":
    parse_args(sys.argv[1:])
    main(sys.argv[1:], process_env())

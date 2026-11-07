from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from base.events.contract import EVENTS, LLM_ERROR_FAMILY, family_events
from base.paths import meiva_home

pytestmark = pytest.mark.registry
_ROOT = Path(__file__).resolve().parents[3]
_SKIPPED_DIRS = frozenset(
    {".git", ".venv", ".worktrees", "node_modules", "tests", "__pycache__", "migrations"}
)
_EQUALS = re.compile(r"event_name\s*=\s*'([^']+)'")
_IN_LIST = re.compile(r"event_name\s+IN\s*\(([^)]*)\)")
_STARTS_WITH = re.compile(r"starts_with\(\s*(?:\w+\.)?event_name\s*,\s*'([^']+)'\s*\)")
_QUOTED = re.compile(r"'([^']+)'")
_LOGQL_NAMES = re.compile(r"event_name\s*=~?\s*\"([^\"]+)\"")


def _reader_sources() -> list[Path]:
    paths: list[Path] = []
    for directory, subdirectories, files in os.walk(_ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in _SKIPPED_DIRS]
        for name in files:
            if name.endswith(".py") and not name.startswith(("test_", "conftest")):
                path = Path(directory) / name
                if "telemetry_events" in path.read_text(encoding="utf-8"):
                    paths.append(path)
    return paths


def _string_constants(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _queried_by_sql() -> tuple[dict[str, Path], dict[str, Path]]:
    names: dict[str, Path] = {}
    prefixes: dict[str, Path] = {}
    for path in _reader_sources():
        text = _string_constants(path)
        for match in _EQUALS.finditer(text):
            names[match[1]] = path
        for match in _IN_LIST.finditer(text):
            for quoted in _QUOTED.finditer(match[1]):
                names[quoted[1]] = path
        for match in _STARTS_WITH.finditer(text):
            prefixes[match[1]] = path
    return names, prefixes


def _self_evolution_names() -> set[str]:
    path = _ROOT / "meiva_builtins/skills/platform/meiva-self-evolution/scripts/record.py"
    failures: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_EXEC_FAIL_EVENTS" for t in node.targets)
            and isinstance(node.value, ast.Call)
        ):
            declared = node.value.args[0]
            assert isinstance(declared, ast.Set)
            failures = {
                element.value
                for element in declared.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    assert failures, "record.py no longer declares _EXEC_FAIL_EVENTS"
    return {"llm_usage", "turn_end", "code", "exec", "plugin_activation", *failures}


def _parameter_lists() -> dict[str, set[str]]:
    from base.telemetry.metrics import aggregate_sql
    from gateway.lgtm import telemetry_staleness

    return {
        "aggregate_sql.EXEC_FAILURE_EVENTS": set(aggregate_sql.EXEC_FAILURE_EVENTS),
        "aggregate_sql.LIFECYCLE_EVENTS": set(aggregate_sql.LIFECYCLE_EVENTS),
        "ops_series LLM error family": set(family_events(LLM_ERROR_FAMILY)),
        "telemetry_staleness.HEARTBEAT_EVENT": {telemetry_staleness.HEARTBEAT_EVENT},
        "meiva-self-evolution record.py": _self_evolution_names(),
    }


def _inspector_names() -> set[str]:
    from gateway.inspect import _plugin_metrics
    from gateway.inspect._plugin_faces import SHIPPED_PLUGINS_DIR, enabled_plugin_names

    names: set[str] = set()
    for spec in _plugin_metrics.load_plugin_metrics(
        SHIPPED_PLUGINS_DIR, enabled_plugin_names(home=meiva_home())
    ):
        if "inspector" not in spec.output:
            continue
        names.add(spec.event_name)
        for query in [spec.query, *(spec.targets or [])]:
            for group in _LOGQL_NAMES.finditer(str(query)):
                names.update(str(group[1]).split("|"))
    names.discard("{event_name}")
    return names


def _unpersisted(names: set[str]) -> list[str]:
    return sorted(
        name
        for name in names
        if name in EVENTS
        and (
            EVENTS[name].category in ("telemetry", "log")
            or "telemetry" in EVENTS[name].extra_categories
        )
        and not EVENTS[name].persist
    )


def test_the_source_scan_still_finds_the_readers() -> None:
    names, _prefixes = _queried_by_sql()
    assert {"llm_usage", "turn_end", "sse_drop", "service_started", "syntax_fix"} <= set(names)
    assert len(_reader_sources()) >= 10


def test_events_the_sql_readers_name_are_persisted() -> None:
    names, prefixes = _queried_by_sql()
    missing = {name: names[name] for name in _unpersisted(set(names))}
    for prefix, path in prefixes.items():
        for name in _unpersisted({n for n in EVENTS if n.startswith(prefix)}):
            missing[name] = path
    assert not missing, (
        "events a telemetry_events reader queries by name, without persist=True "
        f"(declare it in base/events/declarations/): { {k: str(v.relative_to(_ROOT)) for k, v in missing.items()} }"
    )


def test_name_lists_the_readers_pass_are_persisted() -> None:
    missing = {
        source: _unpersisted(names)
        for source, names in _parameter_lists().items()
        if _unpersisted(names)
    }
    assert not missing, f"events a reader's name list selects, without persist=True: {missing}"


def test_inspector_metrics_read_only_persisted_events() -> None:
    names = _inspector_names()
    assert {"llm_usage", "turn_end", "passive_recall"} <= names
    assert not _unpersisted(names), (
        f"inspector metrics over events without persist=True: {_unpersisted(names)}"
    )


# meiva:note
# Persisted events without a reader, in the manifest's debt shape (`meizel.toml` `[debt.*]`): the
# `owner` paying it down and the `until` that ends it. A bare allowance would be the whitelist the
# judgment below exists to prevent; an entry must still describe a real gap
# (`test_the_reader_debt_still_describes_a_gap`), so this table can only shrink.
_PERSIST_WITHOUT_READER_DEBT: dict[str, dict[str, str]] = {
    "schedule_not_progressing": {
        "owner": "#6400",
        "until": (
            "waits for the A-segment alert consumer (agent #1818) to choose between a Loki rule "
            "and a card that reads the event from the table; only the card reads it by name, so "
            "the entry goes to zero when that decision lands"
        ),
    },
    "llm_usage_metering_failed": {
        "owner": "#6400",
        "until": (
            "waits on the same A-segment decision (agent #1818): the metering failure is an alert "
            "candidate whose consumer, a Loki rule, would not read the table — an entry here only "
            "while the card path is still open"
        ),
    },
}


# meiva:note
# The reader inventory as the three shapes above see it, which is also the set the reverse
# judgment below is allowed to accept: a name in a SQL literal, in one of the name lists the
# readers pass, or in an inspector metric. `main_red_watch_round` is read through a query that
# names it literally for this reason.
def _named_reader_names() -> set[str]:
    names, prefixes = _queried_by_sql()
    selected = set(names) | _inspector_names()
    for listed in _parameter_lists().values():
        selected |= set(listed)
    for prefix in prefixes:
        selected |= {name for name in EVENTS if name.startswith(prefix)}
    return selected


# meiva:note
# The other half of the judgment: `persist` means "a Postgres reader queries it by name"
# (docs/decisions/observability/telemetry/2026-10-03-telemetry-events-stores-what-is-read.md), so
# a persisted event no reader names is a row on the hottest write path that no query answers. A
# reader the source scan cannot name is the declaration this test must fail on, not a detail to
# allow: the one such reader, `main_red_watch_round`'s, now spells its event into the query.
def test_persisted_events_have_a_named_reader() -> None:
    named = _named_reader_names()
    missing = sorted(
        name
        for name, spec in EVENTS.items()
        if spec.persist and name not in named and name not in _PERSIST_WITHOUT_READER_DEBT
    )
    assert not missing, (
        "events marked persist=True that no reader selects by name: "
        f"{missing}. Either a reader selects the name — a SQL literal, a name list, or an "
        "inspector metric — or the declaration drops persist=True: `persist` marks an event a "
        "Postgres reader queries by name, and the JSONL mirror and Loki keep the rest."
    )


# meiva:note
# The debt above only shrinks: an entry whose event gained a reader, lost its `persist`, or left
# the registry is stale, and the fix is to delete it rather than to widen the allowance.
def test_the_reader_debt_still_describes_a_gap() -> None:
    named = _named_reader_names()
    stale = sorted(
        name
        for name, entry in _PERSIST_WITHOUT_READER_DEBT.items()
        if name not in EVENTS
        or not EVENTS[name].persist
        or name in named
        or not entry.get("owner")
        or not entry.get("until")
    )
    assert not stale, (
        "reader-debt entries that no longer describe a persisted event without a reader: "
        f"{stale}. Delete each: the gap it recorded is closed, or the entry lost the owner/until "
        "that make it a debt rather than an allowance."
    )

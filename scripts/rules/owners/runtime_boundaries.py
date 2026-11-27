# meiva:link scripts/rules/docs/rules.md

import ast
import re
from collections.abc import Iterable

from meizel.api import Fact, lint_common

_CLAIM = "agent/graph/claim/"
_ADMISSION = "gateway/message_admission/"
_HISTORY = "base/agents/history/"
_TRANSCRIPT = _HISTORY + "transcript_timeline/"
_SINK = _CLAIM + "harness_read_sink.py"
_USAGE = "base/lm/usage/"
_LEDGER = "base/db/usage_ledger.py"

_TIMELINE = frozenset({_HISTORY + "timeline.py", _HISTORY + "timeline_item.py"})
_RECORD_LOCATION = frozenset({"record_location", "session_file", "unreadable_file"})
_PROJECTION_EVENTS = frozenset({"TimelineSnapshot", "InboundCommitted"})


def _claim_projection(fact: Fact) -> bool:
    if fact.dst == _SINK:
        return fact.src != _CLAIM + "harness_turn.py" or fact.names != ("ReadTimelineSink",)
    if fact.src in {_SINK, _CLAIM + "_present.py"}:
        return False
    if fact.dst == _TRANSCRIPT + "record/harness.py" and fact.src == _CLAIM + "harness_turn.py":
        return not fact.names or not _RECORD_LOCATION.issuperset(fact.names)
    if fact.dst == "base/events/live/projection.py":
        return bool(_PROJECTION_EVENTS.intersection(fact.names))
    return (
        fact.dst in _TIMELINE
        or fact.dst.startswith(_TRANSCRIPT)
        or fact.dst == "agent/graph/llm_stream/harness_overlay.py"
    )


def _admission_projection(fact: Fact) -> bool:
    if fact.src == _ADMISSION + "legacy_quote.py" and fact.dst in _TIMELINE:
        return False
    if fact.dst == _TRANSCRIPT + "__init__.py":
        return fact.names != ("record_quote_coordinates",)
    return (
        fact.dst in _TIMELINE
        or fact.dst.startswith(_TRANSCRIPT)
        or fact.dst.startswith("gateway/agents/history/")
    )


def _direct_usage(fact: Fact) -> bool:
    if fact.dst == _LEDGER:
        return fact.src != "gateway/app.py" or fact.names != ("bind_usage_ledger",)
    if fact.dst == "agent/llm/usage.py":
        return True
    if fact.dst != _USAGE + "__init__.py":
        return False
    sync_emitters = {"log_usage_fields", "log_usage_from_message"}
    if fact.src == "gateway/agents/reply_suggestion/service.py":
        sync_emitters.remove("log_usage_from_message")
    return bool(sync_emitters.intersection(fact.names))


def _repair(fact: Fact) -> str | None:
    if lint_common.is_test_path(fact.src):
        return None
    if fact.src.startswith(_CLAIM) and _claim_projection(fact):
        return "keep timeline projection in the notification-only ReadTimelineSink"
    if fact.src.startswith(_ADMISSION) and _admission_projection(fact):
        return "verify quote membership through the canonical source identity contracts in quote_source.py"
    if fact.dst == _LEDGER and {"insert_usage", "usage_writer"}.intersection(fact.names):
        return "keep ledger inserts in base/db/usage_ledger.py and submit usage receipts on request paths"
    if fact.src.startswith((_CLAIM, "gateway/")) and _direct_usage(fact):
        return "submit usage receipts through the service-owned staging runtime"
    return None


def import_errors(facts: Iterable[Fact]) -> list[str]:
    return sorted(
        {
            f"{fact.src}:{fact.line}: {fact.dst} crosses a runtime boundary; {repair}."
            for fact in facts
            if (repair := _repair(fact)) is not None
        }
    )


_SQL_LEDGER = r'(?:["\w]+\s*\.\s*)?"?llm_usage_ledger"?(?![\w"])'
_LEDGER_INSERT = re.compile(
    rf"\b(?:INSERT\s+INTO|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\s+(?:TABLE\s+)?)\s+"
    rf"(?:ONLY\s+)?{_SQL_LEDGER}(?=\s|\(|;|$)"
    rf"|\bCOPY\s+{_SQL_LEDGER}(?:\s*\([^)]*\))?\s+FROM\b",
    re.IGNORECASE,
)
_LEDGER_REPRICE = re.compile(
    rf"\bUPDATE\s+(?:ONLY\s+)?{_SQL_LEDGER}\s+(?:SET\b|(?:AS\s+)?\w+\s+SET\b)",
    re.IGNORECASE,
)


def _matching_strings(tree: ast.Module, pattern: re.Pattern[str]) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and pattern.search(node.value)
    )


def ledger_inserts(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    return _matching_strings(tree, _LEDGER_INSERT)


def ledger_updates(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    return _matching_strings(tree, _LEDGER_REPRICE)


def ledger_writer_access(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "write_usage_row"
    )


_MIRROR_PATH = re.compile(r"(?:^|/)bootstrap-mirror(?:\.json|-enrolled)$")


def mirror_paths(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    return _matching_strings(tree, _MIRROR_PATH)

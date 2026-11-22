from __future__ import annotations

from datetime import UTC, datetime, timedelta

from base.agents.birth.harness import HarnessProbe
from base.agents.birth.unpriced_probe_alert import (
    UnpricedProbeReporter,
    report_unpriced_probed_models,
)
from base.api_contracts.agent_contract import Harness, ProbedHarnessModel
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records

_NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def test_report_unpriced_probed_models_deduplicates_within_cooldown_and_normalizes(
    loguru_records: LogRecords,
) -> None:
    reporter = UnpricedProbeReporter(cooldown=timedelta(hours=24))
    known = frozenset({"gemini-4-neon", "claude-fable-5-2", "gpt-6.2-sol"})
    probes = [
        HarnessProbe(Harness.MEIVA, "available", "built-in"),
        HarnessProbe(
            Harness.ANTIGRAVITY,
            "available",
            "ok",
            models=(
                ProbedHarnessModel(id="gemini-4-neon"),
                ProbedHarnessModel(id="gemini-5-quasar-high"),
                ProbedHarnessModel(id="gemini-5-quasar-medium"),
            ),
        ),
        HarnessProbe(
            Harness.CLAUDE_CODE,
            "available",
            "ok",
            models=(
                ProbedHarnessModel(id="sonnet[1M]"),
                ProbedHarnessModel(id="claude-fable-5-2[1m]"),
                ProbedHarnessModel(id="claude-fable-5-2"),
                ProbedHarnessModel(id="claude-fable-6[1m]"),
            ),
        ),
        HarnessProbe(
            Harness.CODEX,
            "available",
            "ok",
            models=(ProbedHarnessModel(id="gpt-6.2-sol"),),
        ),
    ]

    first = report_unpriced_probed_models(
        probes, known_priced_models=known, now=_NOW, reporter=reporter
    )
    assert first == (
        (Harness.ANTIGRAVITY, "gemini-5-quasar"),
        (Harness.CLAUDE_CODE, "claude-fable-6"),
    )

    second = report_unpriced_probed_models(
        probes,
        known_priced_models=known,
        now=_NOW + timedelta(hours=23, minutes=59),
        reporter=reporter,
    )
    assert second == ()

    third = report_unpriced_probed_models(
        probes,
        known_priced_models=known,
        now=_NOW + timedelta(hours=24),
        reporter=reporter,
    )
    assert third == (
        (Harness.ANTIGRAVITY, "gemini-5-quasar"),
        (Harness.CLAUDE_CODE, "claude-fable-6"),
    )

    events = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "harness_model_unpriced_probed"
    ]
    assert len(events) == 4
    assert events[0]["harness"] == "antigravity"
    assert events[0]["raw_model"] == "gemini-5-quasar-high"
    assert events[0]["canonical_model"] == "gemini-5-quasar"
    assert events[1]["harness"] == "claude_code"
    assert events[1]["raw_model"] == "claude-fable-6[1m]"
    assert events[1]["canonical_model"] == "claude-fable-6"


def test_report_unpriced_probed_models_process_lifetime_dedup_without_clock(
    loguru_records: LogRecords,
) -> None:
    reporter = UnpricedProbeReporter()
    known = frozenset({"gemini-4-neon"})
    probes = [
        HarnessProbe(
            Harness.ANTIGRAVITY,
            "available",
            "ok",
            models=(ProbedHarnessModel(id="gemini-5-nova-high"),),
        )
    ]

    assert report_unpriced_probed_models(probes, known_priced_models=known, reporter=reporter) == (
        (Harness.ANTIGRAVITY, "gemini-5-nova"),
    )
    assert report_unpriced_probed_models(probes, known_priced_models=known, reporter=reporter) == ()
    assert len(loguru_records.events("harness_model_unpriced_probed")) == 1

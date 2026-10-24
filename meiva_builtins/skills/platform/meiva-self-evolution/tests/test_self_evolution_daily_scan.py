# meiva:note
# Unit tests for the self-evolution daily scan (scripts/daily_scan.py, scripts/scan_report.py).
#
# The skill scripts are standalone (the skill dir has a hyphen, so they are not importable as a package); load the real script by path with the shared test loader. The report and the verdict are functions of values (`scan_report`); the scan itself runs against a real events listener on port 0 and the throwaway Postgres.

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from base.db import Database
from meiva_builtins.skills.tests.skill_script import load_skill_script

world = load_skill_script("platform", "meiva-self-evolution", "tests", "collection_world.py")
FLOOR = 0.5
NOW = world.ts_after(3600)


@pytest.fixture()
def daily_scan() -> Any:
    """Load independently of collection imports and restore sibling-import paths."""
    before = sys.path[:]
    try:
        return cast(
            Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "daily_scan.py")
        )
    finally:
        sys.path[:] = before


@pytest.fixture()
def report() -> Any:
    """The report and verdict module, loaded by path."""
    before = sys.path[:]
    try:
        return cast(
            Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "scan_report.py")
        )
    finally:
        sys.path[:] = before


def _view(
    report: Any,
    path: Path = Path("daily.jsonl"),
    counts: dict[str, int] | None = None,
    *,
    days: int = 1,
    now: datetime = NOW,
    floor: float = FLOOR,
) -> Any:
    return report.ScanView(path, days, counts, now, floor)


def _record(label: str, agent_id: int = 1, **overrides: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "agent_id": agent_id,
        "week": "2026-08-13",
        "spawner": "user",
        "task_prompt": "",
        "followup_prompts": [],
        "corrections": [],
        "peer_feedback": [],
        "transcript": [],
        "final_output": "done",
        "turns": 3,
        "exec_ok": 1,
        "exec_failed": 0,
        "tokens_in": 1_000,
        "tokens_out": 500,
        "last_exec_failed": False,
        "compactions": 0,
        "terminated": False,
        "evidence_grade": "complete",
        "label": label,
    }
    rec.update(overrides)
    return rec


def _exit(
    report: Any, records: list[dict[str, object]], counts: dict[str, int] | None = None
) -> int:
    return report.alert_verdict(records, counts, FLOOR).code


def test_alert_exit_is_2_when_any_run_is_bad(report: Any) -> None:
    assert _exit(report, [_record("ok"), _record("ok")]) == 0
    assert _exit(report, [_record("ok"), _record("fumbled")]) == 2
    assert _exit(report, [_record("failed")]) == 2


def test_alert_exit_is_2_when_no_runs_collected(report: Any) -> None:
    """An empty dataset means the data source broke — never "nothing to act
    on" (2026-08-14: PG events froze and the scan green-lit an empty day)."""
    assert _exit(report, []) == 2
    rendered = report.render([], _view(report, Path("daily-2026-08-14.jsonl")))
    assert "0 runs collected" in rendered


def test_render_lists_bad_runs_with_their_signals(report: Any) -> None:
    records = [
        _record("ok", agent_id=1),
        _record(
            "failed",
            agent_id=2,
            task_prompt="Fix the suite",
            corrections=["\u4e0d\u5bf9\uff0c\u91cd\u505a"],
            exec_failed=4,
            last_exec_failed=True,
        ),
    ]
    out = report.render(records, _view(report))
    assert "runs: 2 (ok 1 / fumbled 0 / failed 1 / unknown 0)" in out
    assert "ALERT — 1 run(s) worth mining:" in out
    assert "#2 failed" in out
    assert "1 user correction(s)" in out
    assert "4 failed exec(s)" in out
    assert "last exec failed" in out
    assert "Fix the suite" in out


def test_render_is_quiet_on_clean_day(report: Any) -> None:
    out = report.render([_record("ok", agent_id=1), _record("ok", agent_id=2)], _view(report))
    assert "ALERT" not in out


def test_render_header_carries_the_clock_reading_it_was_given(report: Any) -> None:
    out = report.render([_record("ok")], _view(report, now=world.ts_after(86400 + 7)))

    assert out.splitlines()[0] == "self-evolution daily scan — 2026-08-14T00:00:07Z"


def test_render_reports_subprocess_and_shell_run_totals(report: Any) -> None:
    out = report.render(
        [
            _record("ok", subprocess_calls=3, tools_called={"meiva.shell.run": 5}),
            _record("ok", agent_id=2, subprocess_calls=4, tools_called={"meiva.shell.run": 6}),
        ],
        _view(report),
    )

    assert "subprocess calls: 7 (shell.run 11)" in out.splitlines()


def test_alert_exit_is_0_on_test_only_window(report: Any) -> None:
    """A window whose only activity was TEST- spawns is a quiet day, not a
    data-source outage: the filter removed every run by design, so the
    empty-dataset sentinel must not fire (QA review of PR #698)."""
    counts = {"seen": 3, "excluded_test": 3, "skipped_meta": 0}
    assert _exit(report, [], counts) == 0
    rendered = report.render([], _view(report, Path("daily-test-only.jsonl"), counts))
    assert "ALERT" not in rendered
    assert "0 runs collected" not in rendered
    assert "3 window agent(s) were TEST- spawns" in rendered


def test_alert_exit_stays_2_on_zero_seen_runs(report: Any) -> None:
    """The `seen > 0` guard is load-bearing: a truly empty source
    (seen=0 — the 2026-08-14 outage shape) must stay ALERT, never read as
    a TEST- only window (0 == 0 without the guard)."""
    counts = {"seen": 0, "excluded_test": 0, "skipped_meta": 0}
    assert _exit(report, [], counts) == 2
    rendered = report.render([], _view(report, Path("d.jsonl"), counts))
    assert "ALERT — 0 runs collected" in rendered


def test_alert_exit_stays_2_when_seen_runs_vanish_without_test_filter(report: Any) -> None:
    """The sentinel keys on pre-filter activity: a window whose seen runs
    produced no records for any other reason (e.g. every lifecycle row
    missing) is still an anomaly, never a quiet day."""
    counts = {"seen": 2, "excluded_test": 0, "skipped_meta": 2}
    assert _exit(report, [], counts) == 2
    rendered = report.render([], _view(report, Path("d.jsonl"), counts))
    assert "ALERT — 0 runs collected" in rendered


def test_render_compacts_oversized_bad_list_when_report_is_persisted(report: Any) -> None:
    """A bad list over the stdout budget (the runner delivers only the output
    tail) collapses to counter lines once the full report is on disk, so the
    trailing summary + pointer always survive in the delivered tail."""
    records = [
        _record(
            "fumbled", agent_id=i, task_prompt=f"long task {i} " + "x" * 140, corrections=["fix"]
        )
        for i in range(1, 21)
    ]
    out = report.render(
        records, _view(report, Path("d.jsonl")), report_path=Path("d.report.txt"), compact_bad=True
    )
    assert "(compact; details in report)" in out
    assert "| task:" not in out
    assert "  #1 fumbled c1" in out
    assert "full report: d.report.txt" in out
    assert len(out) < 2000


def test_render_keeps_rich_bad_lines_within_budget(report: Any) -> None:
    """A small bad list stays rich even under compact_bad: compaction is a
    fallback for oversized lists only."""
    records = [
        _record("fumbled", agent_id=1, task_prompt="short task"),
        _record("fumbled", agent_id=2, task_prompt="another task"),
        _record("failed", agent_id=3, task_prompt="third task"),
    ]
    out = report.render(
        records, _view(report, Path("d.jsonl")), report_path=Path("d.report.txt"), compact_bad=True
    )
    assert "(compact" not in out
    assert "| task: short task" in out
    assert "full report: d.report.txt" in out


def test_render_appends_summary_and_report_pointer_last(report: Any) -> None:
    """The closing two lines carry the window summary and the persisted
    report path — the part tail truncation must keep."""
    records = [
        _record("ok", agent_id=1),
        _record("fumbled", agent_id=2, corrections=["a", "b"], peer_feedback=["p"]),
        _record("failed", agent_id=3, exec_failed=2),
    ]
    out = report.render(records, _view(report, Path("d.jsonl")), report_path=Path("d.report.txt"))
    lines = out.splitlines()
    assert lines[-2] == (
        "summary: 3 runs (ok 1 / fumbled 1 / failed 1 / unknown 0)"
        " | corrections 2 | peer 1 | exec-fail runs 1"
    )
    assert lines[-1] == "full report: d.report.txt"


def test_render_defaults_unchanged(report: Any) -> None:
    """No kwargs keeps the pre-existing contract: no summary, no pointer, no
    compaction even for an oversized bad list."""
    records = [
        _record("fumbled", agent_id=i, task_prompt=f"long task {i} " + "x" * 140)
        for i in range(1, 21)
    ]
    out = report.render(records, _view(report, Path("d.jsonl")))
    assert "summary:" not in out
    assert "full report:" not in out
    assert "(compact" not in out
    assert "| task:" in out


def test_render_never_compacts_without_report_path(report: Any) -> None:
    """Compacting without a stored report would drop the only copy of the
    task text, so the gate is report_path — compact_bad alone is not enough."""
    records = [
        _record("fumbled", agent_id=i, task_prompt=f"long task {i} " + "x" * 140)
        for i in range(1, 21)
    ]
    out = report.render(records, _view(report, Path("d.jsonl")), compact_bad=True)
    assert "(compact" not in out
    assert "| task:" in out


def test_compact_bad_line_pins_every_signal_token(report: Any) -> None:
    """The compact tokens mirror _why() one-for-one — pin the full mapping so
    a dropped or renamed signal cannot pass unnoticed (QA review of #2366)."""
    all_signals = _record(
        "fumbled",
        agent_id=7,
        followup_prompts=["a", "b"],
        corrections=["c"],
        peer_feedback=["p"],
        exec_failed=2,
        last_exec_failed=True,
        compactions=3,
        terminated=True,
        final_output="",
        turns=1,
    )
    assert report._compact_bad_line(all_signals) == "  #7 fumbled rep2 c1 pf1 ef2 last cp3 term"
    assert report._compact_bad_line(_record("failed", agent_id=8)) == "  #8 failed -"


def test_render_reports_the_unknown_bucket_with_reasons(report: Any) -> None:
    records = [
        _record("ok", agent_id=1),
        _record("fumbled", agent_id=2, corrections=["fix"]),
        _record("failed", agent_id=3, exec_failed=4),
        _record("unknown", agent_id=4, unknown_reason="no_telemetry"),
    ]

    out = report.render(records, _view(report, Path("daily.jsonl")))

    assert "runs: 4 (ok 1 / fumbled 1 / failed 1 / unknown 1 (no_telemetry 1))" in out
    assert "ALERT — 2 run(s) worth mining:" in out
    assert "#4 unknown" not in out


def test_unknown_runs_stay_out_of_the_bad_list(report: Any) -> None:
    records = [
        _record("ok", agent_id=1),
        _record("unknown", agent_id=2, unknown_reason="no_telemetry", evidence_grade="partial"),
    ]

    out = report.render(records, _view(report, Path("daily.jsonl")))

    assert "worth mining" not in out
    assert "#2 unknown" not in out
    assert "unknown 1 (no_telemetry 1)" in out


def test_unknown_share_below_the_floor_keeps_the_scan_quiet(report: Any) -> None:
    records = [_record("ok", agent_id=i) for i in range(1, 10)] + [
        _record("unknown", agent_id=10, unknown_reason="no_task", evidence_grade="partial")
    ]

    assert report.alert_verdict(records, None, FLOOR).code == 0
    assert report.alert_verdict(records, None, FLOOR).reason is None


# meiva:note
# 90% judgeable is quiet under the default floor and a data-quality alert under a stricter one.
def test_the_coverage_floor_is_the_value_the_caller_passes(report: Any) -> None:
    records = [_record("ok", agent_id=i) for i in range(1, 10)] + [
        _record("unknown", agent_id=10, unknown_reason="no_task", evidence_grade="partial")
    ]

    strict = report.alert_verdict(records, None, 0.95)
    out = report.render(records, _view(report, floor=0.95))

    assert strict == (2, "data-quality: low-coverage")
    assert "judgeable 90.0% < floor 95%" in out


def test_coverage_headline_reports_the_judgeable_share(report: Any) -> None:
    records = [_record("ok", agent_id=i) for i in range(1, 106)] + [
        _record("unknown", agent_id=i, unknown_reason="no_telemetry", evidence_grade="none")
        for i in range(106, 893)
    ]

    out = report.render(records, _view(report, Path("daily.jsonl")))

    assert "batch 892 runs · judgeable 105 (11.8%)" in out
    assert "unknown 787 (no_telemetry 787)" in out
    assert " · scored " in out


def test_low_coverage_alerts_with_the_data_quality_reason(report: Any) -> None:
    records = [_record("ok", agent_id=1)] + [
        _record("unknown", agent_id=i, unknown_reason="no_terminal", evidence_grade="partial")
        for i in range(2, 11)
    ]

    verdict = report.alert_verdict(records, None, FLOOR)
    out = report.render(records, _view(report, Path("daily.jsonl")))

    assert verdict == (2, "data-quality: low-coverage")
    assert "ALERT — data-quality: low-coverage" in out
    assert "largest unknown gap no_terminal (9 run(s))" in out


def test_empty_window_alerts_as_an_empty_window(report: Any) -> None:
    assert report.alert_verdict([], None, FLOOR) == (2, "empty-window")
    assert _exit(report, []) == 2
    assert "0 runs collected" in report.render([], _view(report, Path("d.jsonl")))


def test_regression_keeps_its_own_reason(report: Any) -> None:
    assert report.alert_verdict([_record("ok"), _record("failed", agent_id=2)], None, FLOOR) == (
        2,
        "regression",
    )


def test_test_only_window_still_exits_zero(report: Any) -> None:
    counts = {"seen": 3, "excluded_test": 3, "skipped_meta": 0}

    assert report.alert_verdict([], counts, FLOOR) == (0, None)


# meiva:note
# A terminated agent in the database and the events it emitted. A failed run ended with no output after a turn; a plain one delivered.
def _seed_run(
    cur: psycopg.Cursor, agent_id: int, *, label: str | None = None, failed: bool = False
) -> list[dict[str, object]]:
    world.insert_agent(
        cur,
        agent_id,
        label=label,
        status="terminated",
        last_message=None if failed else "delivered",
    )
    world.insert_inbound(cur, agent_id, "user", "t" * 200, "1 hour")
    base_id = agent_id * 10
    return [
        world.event_row(
            base_id,
            world.ts_after(10),
            agent_id=agent_id,
            event_name="llm_usage",
            attributes={"model": "m", "in_total": 100, "out_total": 20},
        ),
        world.event_row(
            base_id + 1,
            world.ts_after(20),
            agent_id=agent_id,
            event_name="turn_end",
            attributes={"duration_seconds": 2},
        ),
    ]


def _env(daily_scan: Any, server: Any, home: Path, database: Database) -> Any:
    collection = daily_scan.collect.CollectEnv(
        database=database, gateway=world.grant(server.url), now=NOW, home=home
    )
    return daily_scan.ScanEnv(collection=collection, coverage_floor=FLOOR)


# meiva:note
# `--include-test` is measurement-only: the nightly scan must stay on the default (exclude), and the flag must reach the collection untouched.
def test_scan_passes_include_test_flag_through(
    daily_scan: Any, db_conn: psycopg.Connection, tmp_path: Path, database: Database
) -> None:
    with db_conn.cursor() as cur:
        rows = _seed_run(cur, 1) + _seed_run(cur, 2, label="TEST-bench-on-t1")
    db_conn.commit()

    with world.serve_events(rows) as server:
        env = _env(daily_scan, server, tmp_path, database).collection
        result1 = daily_scan.scan(1, week="w1", env=env)
        result2 = daily_scan.scan(2, week="w2", env=env, include_test=True)

    assert [r["agent_id"] for r in result1.records] == [1]
    assert result1.counts == {"seen": 2, "excluded_test": 1, "skipped_meta": 0}
    assert [r["agent_id"] for r in result2.records] == [1, 2]
    assert result2.counts == {"seen": 2, "excluded_test": 0, "skipped_meta": 0}
    # the default run wrote the dataset file, the measurement run too
    daily = tmp_path / "self_evolution" / "daily"
    assert result1.path == daily / "w1.jsonl"
    assert [json.loads(line)["agent_id"] for line in result1.path.read_text().splitlines()] == [1]
    assert [json.loads(line)["agent_id"] for line in result2.path.read_text().splitlines()] == [
        1,
        2,
    ]


def test_scan_names_the_dataset_after_the_week_it_is_given(
    daily_scan: Any, tmp_path: Path, database: Database
) -> None:
    with world.serve_events([]) as server:
        result = daily_scan.scan(
            1, "2026-08-13", env=_env(daily_scan, server, tmp_path, database).collection
        )

    assert result.path == tmp_path / "self_evolution" / "daily" / "2026-08-13.jsonl"
    assert result.records == []
    assert result.counts == {"seen": 0, "excluded_test": 0, "skipped_meta": 0}


def test_main_persists_report_and_prints_pointer(
    daily_scan: Any,
    report: Any,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() writes the rich report beside the dataset and prints the compact
    stdout view ending in the pointer line."""
    with db_conn.cursor() as cur:
        rows = _seed_run(cur, 1, failed=True) + _seed_run(cur, 2)
    db_conn.commit()

    with world.serve_events(rows) as server:
        code = daily_scan.main([], _env(daily_scan, server, tmp_path, database))

    assert code == 2
    daily = tmp_path / "self_evolution" / "daily"
    dataset = daily / "2026-08-13.jsonl"
    report_file = daily / "2026-08-13.report.txt"
    records = [json.loads(line) for line in dataset.read_text().splitlines()]
    assert sorted(r["label"] for r in records) == ["failed", "ok"]
    assert (
        report_file.read_text(encoding="utf-8").splitlines()
        == report.render(records, _view(report, dataset)).splitlines()
    )
    assert sorted(p.name for p in daily.iterdir()) == [dataset.name, report_file.name]
    out = capsys.readouterr().out
    assert f"full report: {report_file}" in out
    assert "ALERT — 1 run(s) worth mining:" in out


def test_main_exits_zero_on_a_clean_window(
    daily_scan: Any,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with db_conn.cursor() as cur:
        rows = _seed_run(cur, 1)
    db_conn.commit()

    with world.serve_events(rows) as server:
        code = daily_scan.main(["--days", "2"], _env(daily_scan, server, tmp_path, database))

    assert code == 0
    assert "window: 2 day(s)" in capsys.readouterr().out


def test_main_alerts_when_the_source_produced_nothing(
    daily_scan: Any, tmp_path: Path, database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    with world.serve_events([]) as server:
        code = daily_scan.main([], _env(daily_scan, server, tmp_path, database))

    assert code == 2
    assert "ALERT — 0 runs collected" in capsys.readouterr().out


def test_main_rejects_a_window_below_one_day(
    daily_scan: Any, tmp_path: Path, database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    with world.serve_events([]) as server:
        code = daily_scan.main(["--days", "0"], _env(daily_scan, server, tmp_path, database))

    assert code == 1
    assert "error: --days must be >= 1" in capsys.readouterr().err
    assert server.requests == []


def test_main_degrades_to_rich_output_when_report_unwritable(
    daily_scan: Any,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed report publish (tmp + rename) must not compact stdout: the
    rich lines are then the only copy of the details."""
    with db_conn.cursor() as cur:
        rows = _seed_run(cur, 1, failed=True)
    db_conn.commit()
    daily = tmp_path / "self_evolution" / "daily"
    (daily / "2026-08-13.report.txt").mkdir(parents=True)

    with world.serve_events(rows) as server:
        code = daily_scan.main([], _env(daily_scan, server, tmp_path, database))

    captured = capsys.readouterr()
    assert code == 2
    assert "could not write full report" in captured.err
    assert "full report: " not in captured.out
    assert "| task:" in captured.out
    assert sorted(p.name for p in daily.iterdir()) == ["2026-08-13.jsonl", "2026-08-13.report.txt"]


# meiva:note
# The composition root of the standalone scan reads, once, everything the scan then passes down as values: the home, the clock reading, the gateway access, a database handle that connects, and the configured coverage floor.
def test_the_process_root_reads_home_clock_database_and_floor_once(
    daily_scan: Any, db_url: str, unit_home: Path
) -> None:
    before = datetime.now(UTC)

    env = daily_scan.process_env()

    assert env.coverage_floor == FLOOR
    assert env.collection.home == unit_home
    assert before <= env.collection.now <= datetime.now(UTC)
    with env.collection.database.connect() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)

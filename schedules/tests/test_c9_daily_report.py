from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.db import Database
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from schedules.catchup import fire_slot_once

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_PATH = REPO_ROOT / "schedules" / "c9-daily-report-schedule.py"


def _load_schedule_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("c9_daily_report", SCHEDULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _insert_schedule(conn: psycopg.Connection, *, created_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, created_at) "
            "VALUES (%s, 'x', 'python schedule.py', true, 'stopped', %s) RETURNING id",
            (f"c9-test-{created_at.isoformat()}", created_at),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _entry(agent_id: int | None, *, linux: int, macos: int) -> dict[str, int | None]:
    return {
        "run_id": 1 if agent_id is None else agent_id,
        "agent_id": agent_id,
        "linux_minutes": linux,
        "macos_minutes": macos,
    }


class _FakeAccounting(SimpleNamespace):
    def __init__(self, module: ModuleType, *, error: Exception | None = None) -> None:
        super().__init__()
        self.module = module
        self.error = error
        self.windows: list[tuple[str, str]] = []
        self.hosts: list[Host] = []
        self.DEFAULT_REPO = "zhiyuan-zhang0206/Meiva"
        self.DEFAULT_LEDGER = Path("ledger.jsonl")
        self.entries = [_entry(5811, linux=10, macos=0)]

    def collect(
        self, repo: str, since: str, until: str, *, host: Host
    ) -> list[dict[str, int | None]]:
        del repo
        if self.error is not None:
            raise self.error
        self.hosts.append(host)
        self.windows.append((since, until))
        return self.entries

    def append_ledger(self, path: Path, fresh: list[dict[str, int | None]]) -> int:
        del path, fresh
        return 1


# meiva:note
# A materialized template runs from outside the checkout, so the loader must resolve the
# accounting module through the installed package rather than the current directory.
def test_repo_root_survives_runtime_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mat_dir = tmp_path / "schedules" / "11"
    mat_dir.mkdir(parents=True)
    mat_path = mat_dir / SCHEDULE_PATH.name
    shutil.copy(SCHEDULE_PATH, mat_path)
    monkeypatch.chdir(mat_dir)

    spec = importlib.util.spec_from_file_location("c9_daily_materialized", mat_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    saved_path = list(sys.path)
    loaded = module._load_accounting()
    assert Path(loaded.__file__).resolve() == REPO_ROOT / "scripts/ci/pull_requests/accounting.py"
    assert sys.path == saved_path


def test_manifest_declares_c9_daily_report_schedule() -> None:
    manifest = json.loads((REPO_ROOT / "schedules" / "manifest.json").read_text())
    entry = next(
        item for item in manifest["builtin_schedules"] if item["name"] == "c9-daily-report"
    )

    assert SCHEDULE_PATH.is_file()
    assert entry["script"] == SCHEDULE_PATH.name
    assert entry["command"] == "python c9-daily-report-schedule.py"
    assert entry["class"] == "product"
    assert entry["default_enabled"] is True
    assert "05:00 cluster time" in entry["description"]


def test_window_bounds_covers_the_slot_day_in_cluster_timezone() -> None:
    module = _load_schedule_module()
    # meiva:note
    # The day label must follow the cluster wall clock, not the test environment's zone (CI
    # runs UTC): 05:00 Asia/Shanghai on 2026-09-07 is 21:00 UTC on 2026-09-06, so the UTC
    # date and the cluster day differ.
    slot_end = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)

    since, until, day = module.window_bounds(slot_end, cluster_timezone=lambda: "Asia/Shanghai")

    assert since == "2026-09-05T21:00:00Z"
    assert until == "2026-09-06T21:00:00Z"
    assert day == "2026-09-07"


def test_summarize_splits_attribution_and_meters_minutes() -> None:
    module = _load_schedule_module()
    entries = [
        _entry(5811, linux=10, macos=20),
        _entry(405, linux=5, macos=0),
        _entry(None, linux=7, macos=3),
    ]

    payload = module.summarize(
        entries,
        day="2026-09-07",  # time-bomb-ok: explicit fixture window input
        window_start="2026-09-05T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        window_end="2026-09-06T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        appended_runs=2,
    )

    assert payload == {
        "day": "2026-09-07",  # time-bomb-ok: explicit fixture window input
        "window_start": "2026-09-05T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        "window_end": "2026-09-06T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        "runs": 3,
        "attributed_runs": 2,
        "unattributed_runs": 1,
        "total_minutes": 45,
        "attributed_minutes": 35,
        "linux_minutes": 22,
        "macos_minutes": 23,
        "appended_runs": 2,
        "est_usd": round(22 * 0.006 + 23 * 0.062, 2),
    }


def test_fire_reconciles_the_claimed_slot_window_and_emits(
    db_conn: psycopg.Connection,
    database: Database,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    # meiva:note
    # `_fire` derives its window from the claimed slot — the `fire_slot_once` callback
    # argument — and not from the wall clock, which is the layer QA's simulation flagged.
    module = _load_schedule_module()

    def fake_init_gateway(
        name: str, *, producer: object, machine_reader: object, image: LoadedCommit
    ) -> None:
        assert name == module._PROCESS_NAME
        assert callable(producer) and callable(machine_reader)
        assert image.sha is None

    command_host = fake_host()
    accounting = _FakeAccounting(module)
    slot = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 20, 0, tzinfo=UTC))

    assert fire_slot_once(
        database,
        slot,
        None,
        schedule_id=schedule_id,
        fire=partial(
            module._fire,
            producer=lambda: None,
            image=LoadedCommit(REPO_ROOT, None),
            machine_reader=machine_identity.name,
            host=command_host,
            hooks=module.C9FireHooks(
                tz_fn=lambda: "Asia/Shanghai",
                accounting_loader=lambda: accounting,
                gateway_init=fake_init_gateway,
            ),
        ),
    )

    assert accounting.windows == [("2026-09-05T21:00:00Z", "2026-09-06T21:00:00Z")]
    assert accounting.hosts == [command_host]
    [event] = emitted_events
    assert event.category == "telemetry"
    assert event.event_name == "ci_usage_daily"
    attributes = event.attributes
    assert attributes["day"] == "2026-09-07"
    assert attributes["window_start"] == "2026-09-05T21:00:00Z"
    assert attributes["window_end"] == "2026-09-06T21:00:00Z"


def test_fire_reports_failure_without_raising(
    db_conn: psycopg.Connection,
    database: Database,
    machine_identity: MachineIdentity,
) -> None:
    module = _load_schedule_module()
    accounting = _FakeAccounting(module, error=RuntimeError("gh api down"))
    failures: list[str] = []
    slot = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 20, 0, tzinfo=UTC))

    assert fire_slot_once(
        database,
        slot,
        None,
        schedule_id=schedule_id,
        fire=partial(
            module._fire,
            producer=lambda: None,
            image=LoadedCommit(REPO_ROOT, None),
            machine_reader=machine_identity.name,
            host=fake_host(),
            hooks=module.C9FireHooks(
                accounting_loader=lambda: accounting,
                failure_reporter=failures.append,
            ),
        ),
    )

    assert len(failures) == 1
    assert "RuntimeError" in failures[0]
    assert "gh api down" in failures[0]
    assert not fire_slot_once(
        database,
        slot,
        None,
        schedule_id=schedule_id,
        fire=lambda _slot, _payload: pytest.fail("unexpected retry"),
    )
    assert len(failures) == 1


def test_fire_requires_an_explicit_slot() -> None:
    module = _load_schedule_module()
    with pytest.raises(TypeError, match="slot_end"):
        module._fire(_payload=None)


def test_report_agent_override_rejects_nonnumeric_value() -> None:
    module = _load_schedule_module()

    with pytest.raises(
        RuntimeError, match="MEIVA_CI_USAGE_REPORT_AGENT must be a numeric agent id"
    ):
        module._report_agent(env={module._REPORT_AGENT_ENV: "not-an-id"})


def test_report_failure_uses_exact_label_and_existing_message() -> None:
    module = _load_schedule_module()
    unrelated = SimpleNamespace(agent_id=31, label="another", status=module.S.RUNNING)
    target = SimpleNamespace(agent_id=17, label=module._REPORT_LABEL, status=module.S.TERMINATED)
    find = Mock(
        side_effect=[
            SimpleNamespace(agents=[unrelated], next_cursor=31),
            SimpleNamespace(agents=[target], next_cursor=None),
        ]
    )
    send = Mock()

    module._report_failure(
        "collector failed",
        env={},
        list_agents_fn=find,
        send_message_fn=send,
    )

    assert [call.kwargs for call in find.call_args_list] == [
        {"scope": "all", "query": module._REPORT_LABEL, "before_id": None},
        {"scope": "all", "query": module._REPORT_LABEL, "before_id": 31},
    ]
    send.assert_called_once_with(
        17,
        "C9 daily reconciliation failed:\ncollector failed\n"
        "Check the schedule log; backfill the missed window manually with "
        "`scripts/ci/pull_requests/accounting.py --since ... --until ... --append-ledger`.",
    )


def test_load_accounting_resolves_under_scripts_ci() -> None:
    module = _load_schedule_module()
    saved_path = list(sys.path)
    loaded = module._load_accounting()
    assert (
        Path(loaded.__file__).resolve()
        == REPO_ROOT / "scripts" / "ci" / "pull_requests" / "accounting.py"
    )
    assert sys.path == saved_path

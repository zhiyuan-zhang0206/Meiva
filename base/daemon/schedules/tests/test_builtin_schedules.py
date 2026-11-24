from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from base.clock.recurring import RecurringTrigger, RecurringTriggerKind
from base.daemon.schedules.builtin_schedules import ManifestError, load_manifest


def _manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    for e in entries:
        e.setdefault("trigger_kind", "every")
        e.setdefault("interval_seconds", 60)
        script = e["script"]
        assert isinstance(script, str)
        (tmp_path / script).write_text("print('ok')\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": 1, "builtin_schedules": entries})
    )
    return tmp_path / "manifest.json"


class TestLoadManifest:
    def test_loads_repo_manifest(self) -> None:
        scheds = load_manifest()
        by_name = {s.name: s for s in scheds}
        assert {name: s.klass for name, s in by_name.items()} == {
            "c9-daily-report": "product",
            "debt-sweep-daily": "product",
            "main-red-watch": "product",
            "adversarial-eval-weekly": "product",
            "self-evolution-weekly": "product",
            "self-evolution-daily": "product",
            "memory-arbiter": "product",
            "model-update-tracker": "product",
            "harness-cli-update": "product",
            "claude-auth-keepalive": "product",
            "sched-dry-import-weekly": "operator",
            "log-cred-mask-monthly": "operator",
            "qa-keepalive": "operator",
            "agent-watch": "operator",
        }
        assert {name for name, s in by_name.items() if s.default_enabled} == {
            "c9-daily-report",
            "debt-sweep-daily",
            "adversarial-eval-weekly",
            "self-evolution-weekly",
            "self-evolution-daily",
            "memory-arbiter",
            "model-update-tracker",
            "harness-cli-update",
            "claude-auth-keepalive",
            "main-red-watch",
        }

    def test_unknown_class_fails_fast(self, tmp_path: Path) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "x",
                    "class": "mystery",
                    "default_enabled": True,
                    "script": "x.py",
                    "command": "python x.py",
                }
            ],
        )
        with pytest.raises(ManifestError, match="unknown class"):
            load_manifest(path)

    def test_unknown_placement_fails_fast(self, tmp_path: Path) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "x",
                    "class": "product",
                    "default_enabled": True,
                    "script": "x.py",
                    "command": "python x.py",
                    "placement": "unsupported",
                }
            ],
        )
        with pytest.raises(ManifestError, match="unknown placement"):
            load_manifest(path)

    def test_placement_per_machine(self) -> None:
        scheds = load_manifest()
        by_name = {s.name: s for s in scheds}
        assert by_name["harness-cli-update"].placement == "per_machine"
        assert by_name["claude-auth-keepalive"].placement == "per_machine"
        assert by_name["c9-daily-report"].placement == "gateway"

    def test_template_for_schedule_name(self) -> None:
        from base.daemon.schedules.builtin_schedules import template_for_schedule_name

        manifest = load_manifest()
        harness_sched = template_for_schedule_name("harness-cli-update:runner-1", manifest)
        assert harness_sched is not None
        assert harness_sched.name == "harness-cli-update"
        keepalive_sched = template_for_schedule_name("claude-auth-keepalive:runner-1", manifest)
        assert keepalive_sched is not None
        assert keepalive_sched.name == "claude-auth-keepalive"
        c9_sched = template_for_schedule_name("c9-daily-report", manifest)
        assert c9_sched is not None
        assert c9_sched.name == "c9-daily-report"
        assert template_for_schedule_name("unknown-schedule", manifest) is None

    def test_missing_script_file_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(
            json.dumps(
                {
                    "builtin_schedules": [
                        {
                            "name": "x",
                            "class": "product",
                            "default_enabled": True,
                            "script": "nope.py",
                            "command": "python nope.py",
                            "trigger_kind": "every",
                            "interval_seconds": 60,
                        }
                    ]
                }
            )
        )
        with pytest.raises(ManifestError, match="does not exist"):
            load_manifest(path)


class TestRecurringTrigger:
    def test_every_advances_from_now(self) -> None:
        now = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
        trigger = RecurringTrigger(kind="every", interval_seconds=90)

        assert trigger.next_launch(now=now, cluster_timezone="Asia/Shanghai") == datetime(
            2026, 11, 17, 3, 1, 30, tzinfo=UTC
        )

    def test_cron_uses_the_resolved_cluster_timezone(self) -> None:
        now = datetime(2026, 11, 16, 22, 30, tzinfo=UTC)
        trigger = RecurringTrigger(kind="cron", cron_expr="0 7 * * *")

        assert trigger.next_launch(now=now, cluster_timezone="Asia/Shanghai") == datetime(
            2026, 11, 16, 23, 0, tzinfo=UTC
        )

    def test_rejects_an_unknown_runtime_kind(self) -> None:
        trigger = RecurringTrigger(kind=cast(RecurringTriggerKind, "daemon"), interval_seconds=60)

        with pytest.raises(ValueError, match="trigger kind"):
            trigger.validate()


# meiva:note
# Every built-in schedule's `script` must resolve next to the manifest and its `command` must run
# that same path. A schedule whose command names a different file installs and then never starts,
# which is the failure this repository has already paid for once: "merged" is not "running".
def test_every_builtin_command_runs_the_script_the_manifest_names() -> None:
    schedules_dir = Path(__file__).resolve().parents[4] / "schedules"
    for schedule in load_manifest():
        script_path = schedules_dir / schedule.script
        assert script_path.is_file(), f"{schedule.name}: {schedule.script} does not exist"
        assert schedule.command.split()[-1] == schedule.script, (
            f"{schedule.name}: command {schedule.command!r} does not run {schedule.script!r}"
        )

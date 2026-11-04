# meiva:note
# `meiva logs maintenance` is the daily job's single entry point, and the reason the job
# command carries no policy: the retention window is read when the command runs, so a
# release cannot rewrite the operator's decision by regenerating a plist.

import datetime
import os
from pathlib import Path

import pytest

_NOW = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)
POLICY = "agent=7,shell=7,gateway=7,ops=7,watchdog=7,snapshot=7,other=3"


def _file_at(directory: Path, name: str, mtime: datetime.datetime) -> Path:
    path = directory / name
    path.write_text(name, encoding="utf-8")
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_maintenance_uses_the_policy_it_is_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.observability.logs import cmd_logs_maintenance

    rc = cmd_logs_maintenance(family_days_text=POLICY, logs_path=tmp_path, now=_NOW)

    assert rc == 0
    assert "retention_catch_all\tservices=-\tdays=3" in capsys.readouterr().out


def test_maintenance_rejects_a_policy_it_cannot_parse(tmp_path: Path) -> None:
    from cli.commands.observability.logs import cmd_logs_maintenance

    with pytest.raises(ValueError, match="family must be"):
        cmd_logs_maintenance(family_days_text="nonsense=7", logs_path=tmp_path, now=_NOW)


def test_maintenance_reports_failure_when_rotation_cannot_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.observability.logs import cmd_logs_maintenance

    logs = tmp_path / "logs"
    logs.mkdir()
    _file_at(logs, "meiva-agent-12.out.log", _NOW - datetime.timedelta(days=1))
    # meiva:note
    # A directory rotation cannot rename inside is the honest failure path: no monkeypatch,
    # and it exercises the same branch a full disk would.
    logs.chmod(0o500)
    try:
        rc = cmd_logs_maintenance(family_days_text=POLICY, logs_path=logs, now=_NOW)
    finally:
        logs.chmod(0o700)

    assert rc != 0
    assert "retention_summary" not in capsys.readouterr().out

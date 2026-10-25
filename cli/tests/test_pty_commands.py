from __future__ import annotations

from pathlib import Path

import pytest

from base.paths import meiva_home
from base.paths.tests.homes import point_home
from base.sessions.pty import allocation_freeze
from cli import main as cli_main


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    point_home(monkeypatch, tmp_path / "home")


def test_operator_round_trip_needs_no_gateway_or_data_plane(
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = meiva_home()
    (home / ".env").write_text(
        "MEIVA_DB_URL=postgresql://127.0.0.1:1/meiva?connect_timeout=1\n"
        "MEIVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "MEIVA_GATEWAY_URL=http://127.0.0.1:1\n",
        encoding="utf-8",
    )

    assert (
        cli_main.main(["pty", "freeze", "--holder", "operator-1818", "--reason", "offline cleanup"])
        == 0
    )
    frozen = allocation_freeze.read(home=home)
    assert frozen.generation is not None
    assert cli_main.main(["pty", "status"]) == 0
    assert cli_main.main(["pty", "resume", frozen.generation]) == 0
    assert allocation_freeze.read(home=home).status == "inactive"
    assert "status=frozen" in capsys.readouterr().out


def test_cli_refuses_stale_generation_and_preserves_current_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main.main(["pty", "freeze", "--holder", "current", "--reason", "cleanup"]) == 0
    current = allocation_freeze.read(home=meiva_home())

    assert cli_main.main(["pty", "resume", "stale-generation"]) == 1
    assert allocation_freeze.read(home=meiva_home()) == current
    assert "does not own the freeze" in capsys.readouterr().err


def test_cli_status_surfaces_corrupt_marker_as_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = allocation_freeze.state_path(meiva_home())
    path.write_text("[]")

    assert cli_main.main(["pty", "status"]) == 1
    output = capsys.readouterr().out
    assert "status=invalid" in output
    assert f"marker={path}" in output


def test_cli_resume_reports_allocation_lock_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock = allocation_freeze.lock_path(meiva_home())
    lock.mkdir(parents=True)

    assert cli_main.main(["pty", "resume", "owned-generation"]) == 1
    assert str(lock) in capsys.readouterr().err

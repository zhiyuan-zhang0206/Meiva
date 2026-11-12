# meiva:note
# Real bash sender, durable intent, replay and log retention controls.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from meiva.shell.background import allocate_output_path
from meiva.shell.background_plan import Notice, NotifyPolicy, notified_line
from meiva.shell.completion_plan import Completion, read_completion
from meiva.shell.completion_recovery import recover


def fake_cli(root: Path) -> Path:
    cli = root / "meiva"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "args = sys.argv[1:]\n"
        "with (root/'calls').open('a') as f: f.write(json.dumps(args)+'\\n')\n"
        "key = args[args.index('--client-message-id')+1]\n"
        "db = root/'accepted'\n"
        "keys = set(db.read_text().splitlines()) if db.exists() else set()\n"
        "keys.add(key)\n"
        "db.write_text('\\n'.join(sorted(keys)))\n"
        "sys.exit(1 if (root/'lose-response').exists() else 0)\n"
    )
    cli.chmod(0o755)
    return cli


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("source", ["shell:91", "watcher:91"])
def test_lost_acceptance_replays_same_key_and_preserves_exit(
    tmp_path: Path,
    exit_code: int,
    source: str,
) -> None:
    logs = tmp_path / "5" / ".shell_logs"
    logs.mkdir(parents=True)
    cli = fake_cli(tmp_path)
    (tmp_path / "lose-response").touch()
    log = logs / "91_test.log"
    line = notified_line(
        f"printf 'finished payload\\n'; exit {exit_code}",
        Notice(5, "Test completion", source, cli),
        output_path=log,
        keep=False,
    )
    # meiva:note
    # The generated shell line executes only this test's command and CLI.
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", line], capture_output=True, timeout=5, check=False
    )
    assert result.returncode == exit_code
    marker = log.with_suffix(".undelivered")
    receipt = read_completion(marker.read_text(), marker)
    assert receipt.command_exit == exit_code
    assert f"[exit {exit_code}]" in log.read_text()
    assert "finished payload" in log.read_text()
    # meiva:note
    # A fresh process uses the durable file; the first ambiguous acceptance
    # must not create a second logical delivery on replay.
    (tmp_path / "lose-response").unlink()
    outcome = recover(tmp_path, cli, now=marker.stat().st_mtime + 61)
    assert outcome.delivered == [str(marker)]
    assert not marker.exists()
    calls = [json.loads(row) for row in (tmp_path / "calls").read_text().splitlines()]
    assert calls[0] == calls[1]
    assert len((tmp_path / "accepted").read_text().splitlines()) == 1
    assert recover(tmp_path, cli, now=1e20).delivered == []


def test_success_clears_intent_and_preserves_always_policy(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path)
    log = tmp_path / "success.log"
    line = notified_line(
        "true",
        Notice(5, "Test", "shell:1", cli),
        output_path=log,
        keep=False,
        notify=NotifyPolicy.ALWAYS,
    )
    # meiva:note
    # The generated shell line executes only this test's command and CLI.
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", line], timeout=5, check=False
    )
    assert result.returncode == 0
    assert not log.with_suffix(".undelivered").exists()
    assert "--completion" not in json.loads((tmp_path / "calls").read_text())


def test_retry_budget_spacing_and_corrupt_intent(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path)
    logs = tmp_path / "5" / ".shell_logs"
    logs.mkdir(parents=True)
    markers: list[Path] = []
    for sid in range(3):
        log = logs / f"{sid}_test.log"
        log.touch()
        marker = log.with_suffix(".undelivered")
        marker.write_text(Completion(5, f"shell:{sid}", "done", str(log), True, 0).json())
        markers.append(marker)
    malformed = logs / "bad.undelivered"
    malformed.write_text('{"argv": ["delete", "everything"]}')
    now = max(p.stat().st_mtime for p in markers)
    assert len(recover(tmp_path, cli, now=now).deferred) == 3
    (tmp_path / "lose-response").touch()
    outcome = recover(tmp_path, cli, now=now + 61, limit=1)
    # meiva:note
    # Both the failed send and the malformed intent remain observable.
    assert len(outcome.errors) == 2
    assert len(outcome.deferred) == 2
    assert all(p.exists() for p in markers)
    assert malformed.exists()


def test_pending_log_survives_ring_cap(tmp_path: Path) -> None:
    logs = tmp_path / ".shell_logs"
    logs.mkdir()
    pending = logs / "1_pending.log"
    pending.write_text("completion tail")
    pending.with_suffix(".undelivered").write_text("intent")
    for sid in range(2, 30):
        (logs / f"{sid}_old.log").touch()
    allocate_output_path(30, "new", log_dir=logs, live=())
    assert pending.read_text() == "completion tail"
    assert len(list(logs.glob("*.log"))) == 21


def test_failed_recipient_does_not_starve_the_next_intent(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path)
    logs = tmp_path / "5" / ".shell_logs"
    logs.mkdir(parents=True)
    markers: list[Path] = []
    for sid in range(2):
        log = logs / f"{sid}_test.log"
        log.touch()
        marker = log.with_suffix(".undelivered")
        marker.write_text(Completion(5, f"shell:{sid}", "done", str(log), True, 0).json())
        markers.append(marker)
    now = max(p.stat().st_mtime for p in markers) + 61
    (tmp_path / "lose-response").touch()
    first = recover(tmp_path, cli, now=now, limit=1)
    assert list(first.errors) == [str(markers[0])]
    (tmp_path / "lose-response").unlink()
    second = recover(tmp_path, cli, now=now + 1, limit=1)
    assert second.delivered == [str(markers[1])]
    assert second.deferred == [str(markers[0])]


def test_timeout_retains_intent_and_symlink_is_not_followed(tmp_path: Path) -> None:
    cli = tmp_path / "slow-cli"
    cli.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
    cli.chmod(0o755)
    logs = tmp_path / "5" / ".shell_logs"
    logs.mkdir(parents=True)
    log = logs / "1_test.log"
    log.touch()
    marker = log.with_suffix(".undelivered")
    marker.write_text(Completion(5, "shell:1", "done", str(log), True, 0).json())
    outcome = recover(tmp_path, cli, now=marker.stat().st_mtime + 61, timeout=0.1)
    assert str(marker) in outcome.errors
    assert marker.exists()
    outside = tmp_path / "outside"
    outside.write_text(marker.read_text())
    marker.unlink()
    marker.symlink_to(outside)
    outcome = recover(tmp_path, cli, now=1e20)
    assert "symlink" in outcome.errors[str(marker)]
    assert outside.exists()

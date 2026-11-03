from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.host.env.harness.catalog import (
    CATALOG,
    HarnessSpec,
    Installer,
    needs_cli_update,
    parse_cli_version,
    staged_install_command,
)
from base.host.env.harness.convergence import (
    CommandResult,
    converge_local_harness_clis,
)


class FakeShellRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, Mapping[str, str]]] = []
        self.handlers: list[tuple[Callable[[str], bool], Callable[[str], CommandResult]]] = []

    def handle(
        self,
        matcher: Callable[[str], bool] | str,
        handler: Callable[[str], CommandResult] | CommandResult,
    ) -> None:
        match_fn: Callable[[str], bool] = (
            (lambda cmd: matcher in cmd) if isinstance(matcher, str) else matcher
        )
        handle_fn: Callable[[str], CommandResult] = (
            (lambda _: handler) if isinstance(handler, CommandResult) else handler
        )
        self.handlers.append((match_fn, handle_fn))

    def __call__(self, command: str, *, timeout: float, env: Mapping[str, str]) -> CommandResult:
        self.calls.append((command, timeout, env))
        for matcher, handler in reversed(self.handlers):
            if matcher(command):
                return handler(command)
        return CommandResult(exit_code=0, stdout="", stderr="")


def test_parse_cli_version() -> None:
    assert parse_cli_version("claude 2.1.290") == "2.1.290"
    assert parse_cli_version("agy version 1.4.2\ncommit abc") == "1.4.2"
    assert parse_cli_version("dsh 0.1.5-rc.3") == "0.1.5-rc.3"
    assert parse_cli_version("no version here") == ""


def test_needs_cli_update_unpinned_ordering() -> None:
    agy = CATALOG["agy"]
    assert needs_cli_update(agy, "1.4.1") is True
    assert needs_cli_update(agy, "1.4.2") is False
    assert needs_cli_update(agy, "1.4.10") is False


def test_needs_cli_update_pinned() -> None:
    pinned = CATALOG["claude"]
    assert needs_cli_update(pinned, "") is True
    assert needs_cli_update(pinned, "2.1.289") is True
    assert needs_cli_update(pinned, "2.1.290") is False
    assert needs_cli_update(pinned, "2.1.291") is True


def test_needs_cli_update_unpinned_agy() -> None:
    agy = CATALOG["agy"]
    assert needs_cli_update(agy, "") is True
    assert needs_cli_update(agy, "1.4.1") is True
    assert needs_cli_update(agy, "1.4.2") is False
    assert needs_cli_update(agy, "1.4.3") is False


def test_staged_install_command(tmp_path: Path) -> None:
    agy = CATALOG["agy"]
    target = str(tmp_path / "stage")
    cmd = staged_install_command(agy, target)
    assert "https://antigravity.google/cli/install.sh" in cmd
    assert f"--dir {target}" in cmd

    claude = CATALOG["claude"]
    with pytest.raises(ValueError, match="staged install requires unpinned script installer"):
        staged_install_command(claude, target)


def test_skips_uninstalled_clis_with_zero_shell_calls(tmp_path: Path) -> None:
    user_home = (tmp_path / "home").resolve()
    user_home.mkdir()
    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir()

    absent_spec = HarnessSpec(
        cli="missing_tool",
        runtime=None,
        binary="nonexistent_binary_xyz_123",
        pinned_version="1.0.0",
        installer=Installer(kind="script", target="https://example.com"),
        config=(),
        stores=(),
    )

    runner = FakeShellRunner()
    report = converge_local_harness_clis(
        user_home=user_home,
        meiva_home=meiva_home,
        now=datetime.now(UTC),
        source="converge",
        base_env={"PATH": ""},
        run_shell=runner,
        harnesses=[absent_spec],
    )

    assert report.ok is True
    assert len(report.updated) == 0
    assert len(report.failed) == 0
    assert len(runner.calls) == 0
    assert len(report.outcomes) == 1
    assert report.outcomes[0].status == "skipped_absent"


def test_skips_up_to_date_clis_without_installing(tmp_path: Path) -> None:
    user_home = (tmp_path / "home").resolve()
    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    claude_bin = bin_dir / "claude"
    claude_bin.write_text("#!/bin/sh\n")
    claude_bin.chmod(0o755)

    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir()

    runner = FakeShellRunner()
    runner.handle("claude", CommandResult(exit_code=0, stdout="claude 2.1.290\n", stderr=""))

    report = converge_local_harness_clis(
        user_home=user_home,
        meiva_home=meiva_home,
        now=datetime.now(UTC),
        source="converge",
        base_env={"PATH": str(bin_dir)},
        run_shell=runner,
        harnesses=[CATALOG["claude"]],
    )

    assert report.ok is True
    assert len(report.updated) == 0
    assert len(report.failed) == 0
    assert len(report.outcomes) == 1
    assert report.outcomes[0].status == "up_to_date"
    assert report.outcomes[0].installed_version == "2.1.290"
    assert len(runner.calls) == 1


def test_updates_pinned_npm_cli_with_fallback_prefix(tmp_path: Path) -> None:
    user_home = (tmp_path / "home").resolve()
    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text("#!/bin/sh\n")
    codex_bin.chmod(0o755)

    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir()

    runner = FakeShellRunner()
    runner.handle(
        "npm root -g", CommandResult(exit_code=0, stdout="/usr/local/lib/node_modules\n", stderr="")
    )

    installed_version = "0.160.0"

    def handle_codex(cmd: str) -> CommandResult:
        if "npm install -g" in cmd:
            nonlocal installed_version
            installed_version = "0.166.0"
            return CommandResult(exit_code=0, stdout="added 1 package\n", stderr="")
        if "--version" in cmd:
            return CommandResult(exit_code=0, stdout=f"codex {installed_version}\n", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    runner.handle("codex", handle_codex)

    now = datetime(2026, 11, 3, 4, 30, tzinfo=UTC)
    report = converge_local_harness_clis(
        user_home=user_home,
        meiva_home=meiva_home,
        now=now,
        source="schedule",
        base_env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        run_shell=runner,
        harnesses=[CATALOG["codex"]],
    )

    assert report.ok is True
    assert len(report.updated) == 1
    outcome = report.updated[0]
    assert outcome.cli == "codex"
    assert outcome.previous_version == "0.160.0"
    assert outcome.installed_version == "0.166.0"

    backup = meiva_home / "backups" / "harness-cli" / "codex-0.160.0.bak"
    assert backup.is_file()

    log_file = meiva_home / "onboarding" / "install-log.jsonl"
    assert log_file.is_file()
    lines = log_file.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["harness"] == "codex"
    assert entry["previous"] == "0.160.0"
    assert entry["installed"] == "0.166.0"
    assert entry["source"] == "schedule"


def test_updates_unpinned_agy_atomically_via_staging(tmp_path: Path) -> None:
    user_home = (tmp_path / "home").resolve()
    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    agy_bin = bin_dir / "agy"
    agy_bin.write_text("#!/bin/sh\necho old\n")
    agy_bin.chmod(0o755)

    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir()

    runner = FakeShellRunner()

    def handle_agy(cmd: str) -> CommandResult:
        if "curl -fsSL https://antigravity.google/cli/install.sh" in cmd:
            dir_arg = cmd.split("--dir ")[1].strip().strip("'\"")
            staged = Path(dir_arg) / "agy"
            staged.write_text("#!/bin/sh\necho new\n")
            staged.chmod(0o755)
            return CommandResult(exit_code=0, stdout="installed agy\n", stderr="")
        if "stage" in cmd and "--version" in cmd:
            return CommandResult(exit_code=0, stdout="agy 1.4.2\n", stderr="")
        if "--version" in cmd:
            return CommandResult(exit_code=0, stdout="agy 1.4.1\n", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    runner.handle("agy", handle_agy)

    now = datetime(2026, 11, 3, 4, 30, tzinfo=UTC)
    report = converge_local_harness_clis(
        user_home=user_home,
        meiva_home=meiva_home,
        now=now,
        source="converge",
        base_env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        run_shell=runner,
        harnesses=[CATALOG["agy"]],
    )

    assert report.ok is True
    assert len(report.updated) == 1
    assert report.updated[0].installed_version == "1.4.2"
    assert agy_bin.read_text() == "#!/bin/sh\necho new\n"

    backup = meiva_home / "backups" / "harness-cli" / "agy-1.4.1.bak"
    assert backup.is_file()
    assert backup.read_text() == "#!/bin/sh\necho old\n"


def test_agy_leaves_existing_binary_untouched_on_staging_failure(tmp_path: Path) -> None:
    user_home = (tmp_path / "home").resolve()
    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    agy_bin = bin_dir / "agy"
    agy_bin.write_text("original-content\n")
    agy_bin.chmod(0o755)

    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir()

    runner = FakeShellRunner()
    runner.handle(
        lambda cmd: "--version" in cmd,
        CommandResult(exit_code=0, stdout="agy 1.4.1\n", stderr=""),
    )
    runner.handle(
        "curl -fsSL https://antigravity.google/cli/install.sh",
        CommandResult(exit_code=1, stdout="", stderr="network error sk-1234567890abcdef"),
    )

    report = converge_local_harness_clis(
        user_home=user_home,
        meiva_home=meiva_home,
        now=datetime.now(UTC),
        source="converge",
        base_env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        run_shell=runner,
        harnesses=[CATALOG["agy"]],
    )

    assert report.ok is False
    assert len(report.failed) == 1
    assert "network error sk-***" in report.failed[0].detail
    assert agy_bin.read_text() == "original-content\n"


def test_rollback_on_pinned_cli_failure(tmp_path: Path) -> None:
    user_home = (tmp_path / "home").resolve()
    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    claude_bin = bin_dir / "claude"
    claude_bin.write_text("v2.1.289-bin\n")
    claude_bin.chmod(0o755)

    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir()

    runner = FakeShellRunner()
    runner.handle(
        lambda cmd: "--version" in cmd,
        CommandResult(exit_code=0, stdout="claude 2.1.289\n", stderr=""),
    )
    runner.handle(
        "curl -fsSL https://claude.ai/install.sh",
        CommandResult(exit_code=0, stdout="", stderr=""),
    )

    report = converge_local_harness_clis(
        user_home=user_home,
        meiva_home=meiva_home,
        now=datetime.now(UTC),
        source="converge",
        base_env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        run_shell=runner,
        harnesses=[CATALOG["claude"]],
    )

    assert report.ok is False
    assert len(report.failed) == 1
    assert "restored previous binary from backup" in report.failed[0].detail
    assert claude_bin.read_text() == "v2.1.289-bin\n"

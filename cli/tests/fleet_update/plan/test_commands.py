# meiva:note
# The words each host is sent, and what the window keeps out of its own log. A command is a value:
# these pin the shape every host receives -- the gateway's flags, the checkout order, the
# one-shot job on macOS -- and that a credential never reaches the console or the log.
# meiva:link cli/docs/fleet-update.meiva.okf.md
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cli.fleet_update.oneshot import start_script
from cli.fleet_update.plan.gateway_commands import (
    drift_command,
    machine_name_command,
    pre_verify_script,
    refresh_command,
    roster_command,
)
from cli.fleet_update.plan.host_commands import (
    CLEAN,
    HOME,
    fetch_command,
    helper_probe_command,
    login_shell,
    start_command,
    stop_command,
    switch_command,
)
from cli.fleet_update.plan.redaction import redact
from cli.fleet_update.prestage import prestage_command

_NEW = "0123456789abcdef0123456789abcdef01234567"


def test_a_stop_always_confirms_and_carries_the_flags_it_was_given() -> None:
    assert stop_command("--keep-browser --mode smooth --timeout 60") == (
        f'{HOME}; MEIVA_HOME="$H" "$S/.venv/bin/meiva" stop --yes '
        "--keep-browser --mode smooth --timeout 60"
    )


def test_the_switch_checks_new_out_detached_syncs_the_venv_and_requires_a_clean_tree() -> None:
    command = switch_command(_NEW)
    steps = command.split(" && ")
    assert steps[0].endswith(f"git checkout -q --detach {_NEW}")
    assert steps[1].startswith("find .venv/bin .venv/lib") and "chmod u+w" in steps[1]
    assert (
        steps[2]
        == "env -u VIRTUAL_ENV uv sync --frozen --compile-bytecode --reinstall-package meiva"
    )
    assert steps[3] == CLEAN


def test_the_fetch_is_skipped_when_the_commit_is_already_there() -> None:
    assert fetch_command(_NEW).endswith(
        f'git cat-file -e "{_NEW}^{{commit}}" || git fetch -q origin {_NEW}'
    )


def test_a_start_carries_the_operators_selection() -> None:
    assert start_command("--only-service gateway").endswith(
        'MEIVA_HOME="$H" "$S/.venv/bin/meiva" start --only-service gateway'
    )


@pytest.mark.parametrize(
    ("os_name", "shell"), [("Darwin", "zsh"), ("Linux", "bash"), ("FreeBSD", "bash")]
)
def test_a_host_runs_its_command_in_its_own_login_shell(os_name: str, shell: str) -> None:
    command = login_shell(os_name, "echo 'a b'")
    assert shlex.split(command) == [shell, "-lc", "echo 'a b'"]


def test_the_helper_probe_reads_the_target_commits_objects_not_the_installed_checkout() -> None:
    command = helper_probe_command(_NEW)
    assert (
        f'git -C "$S" archive {_NEW} -- scripts/host_ops/tcc base services/desktop | tar -x -C "$tmp"'
        in command
    )
    assert (
        'PYTHONPATH="$tmp"' in command
        and '"$tmp/scripts/host_ops/tcc/helper-signing.py"' in command
    )
    assert '"$S/scripts/host_ops/tcc/helper-signing.py"' not in command


def test_prestage_warms_dependencies_and_harnesses_from_target_objects_without_mutating_source() -> (
    None
):
    command = prestage_command(_NEW)
    assert "meiva-prestage" in command
    assert f'git -C "$S" archive {_NEW} -- pyproject.toml uv.lock .python-version' in command
    assert f'git -C "$S" diff --quiet HEAD {_NEW} -- uv.lock .python-version' in command
    assert f"diff --quiet HEAD {_NEW} -- pyproject.toml" not in command
    assert 'cp -Rc "$S/.venv" "$tmp/.venv"' in command
    assert "cp -al" not in command
    assert 'UV_PROJECT_ENVIRONMENT="$tmp/.venv"' in command
    assert 'UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-120}"' in command
    assert "uv sync -q --frozen --no-install-project --no-build-package meiva) || exit 1" in command
    assert "pnpm fetch --frozen-lockfile >/dev/null) || exit 1" in command
    assert "converge_local_harness_clis" in command
    assert "git checkout" not in command


@dataclass(frozen=True)
class _PrestageRig:
    tmp_path: Path
    user_home: Path
    home: Path
    repo: Path
    bin_dir: Path
    log_file: Path
    source_py: Path
    source_sentinel: Path
    pyproject_sha: str
    uv_sha: str
    pnpm_sha: str

    def run(self, target: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.user_home),
            "MEIVA_HOME": str(self.home),
            "TMPDIR": str(self.tmp_path),
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            **extra_env,
        }
        env.pop("UV_HTTP_TIMEOUT", None)
        return subprocess.run(
            ["bash", "-c", prestage_command(target)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _git_in(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    ).stdout.strip()


def _build_prestage_rig(tmp_path: Path) -> _PrestageRig:
    user_home = tmp_path / "user-home"
    home = user_home / ".meiva"
    bin_dir, log_file = tmp_path / "bin", tmp_path / "calls.log"
    repo = home / "source"
    (repo / ".venv" / "bin").mkdir(parents=True)
    bin_dir.mkdir()
    (home / ".env").write_text("", encoding="utf-8")
    source_py = repo / ".venv" / "bin" / "python"
    _write_executable(source_py, f'#!/bin/sh\necho "python:$0" >> "{log_file}"\nexit 0\n')
    source_sentinel = repo / ".venv" / "sentinel.txt"
    source_sentinel.write_text("immutable\n", encoding="utf-8")
    _write_executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        f'echo "uv:timeout=$UV_HTTP_TIMEOUT:pyproject=$([ -f pyproject.toml ] && echo yes || echo no)" >> "{log_file}"\n'
        'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
        'rm -f "$UV_PROJECT_ENVIRONMENT/bin/python"\n'
        'printf "#!/bin/sh\\nexit 99\\n" > "$UV_PROJECT_ENVIRONMENT/bin/python"\n'
        'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"\n'
        'exit "${FAKE_UV_RC:-0}"\n',
    )
    _write_executable(
        bin_dir / "pnpm",
        f'#!/bin/sh\necho "pnpm:$*" >> "{log_file}"\nexit "${{FAKE_PNPM_RC:-0}}"\n',
    )

    _git_in(repo, "init", "-q")
    (repo / "pyproject.toml").write_text("[project]\nname = 'meiva'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / ".python-version").write_text("3.12\n", encoding="utf-8")
    (repo / "base" / "host" / "env" / "harness").mkdir(parents=True)
    (repo / "base" / "host" / "env" / "harness" / "convergence.py").write_text(
        "pass\n", encoding="utf-8"
    )
    (repo / "cli").mkdir()
    (repo / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "ui" / "web").mkdir(parents=True)
    (repo / "ui" / "web" / "package.json").write_text("{}\n", encoding="utf-8")
    (repo / "ui" / "web" / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n", encoding="utf-8"
    )
    _git_in(repo, "add", ".")
    _git_in(repo, "commit", "-q", "-m", "base")
    base_sha = _git_in(repo, "rev-parse", "HEAD")

    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'meiva'\n[tool.pyright]\n", encoding="utf-8"
    )
    _git_in(repo, "add", "pyproject.toml")
    _git_in(repo, "commit", "-q", "-m", "pyproject only")
    pyproject_sha = _git_in(repo, "rev-parse", "HEAD")

    (repo / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    _git_in(repo, "add", "uv.lock")
    _git_in(repo, "commit", "-q", "-m", "uv lock change")
    uv_sha = _git_in(repo, "rev-parse", "HEAD")

    (repo / "ui" / "web" / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.1'\n", encoding="utf-8"
    )
    _git_in(repo, "add", "ui/web/pnpm-lock.yaml")
    _git_in(repo, "commit", "-q", "-m", "pnpm lock change")
    pnpm_sha = _git_in(repo, "rev-parse", "HEAD")
    _git_in(repo, "checkout", "-q", base_sha)
    return _PrestageRig(
        tmp_path=tmp_path,
        user_home=user_home,
        home=home,
        repo=repo,
        bin_dir=bin_dir,
        log_file=log_file,
        source_py=source_py,
        source_sentinel=source_sentinel,
        pyproject_sha=pyproject_sha,
        uv_sha=uv_sha,
        pnpm_sha=pnpm_sha,
    )


def test_prestage_shell_skips_uv_on_pyproject_only_diff_and_fails_fast_on_sync_error(
    tmp_path: Path,
) -> None:
    rig = _build_prestage_rig(tmp_path)

    # meiva:note
    # A `pyproject.toml`-only diff skips `uv sync` and keeps using `$S/.venv/bin/python`.
    res_pyproject = rig.run(rig.pyproject_sha)
    assert res_pyproject.returncode == 0
    calls = rig.log_file.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("uv:") for line in calls)
    assert any(line.startswith("python:") for line in calls)

    # meiva:note
    # A failed `uv sync` aborts prestage non-zero instead of falling through to a half-built `.venv`.
    rig.log_file.write_text("", encoding="utf-8")
    res_uv_fail = rig.run(rig.uv_sha, FAKE_UV_RC="28")
    assert res_uv_fail.returncode == 1
    calls = rig.log_file.read_text(encoding="utf-8").splitlines()
    assert calls == ["uv:timeout=120:pyproject=yes"]
    assert rig.source_sentinel.read_text(encoding="utf-8") == "immutable\n"
    assert "exit 0" in rig.source_py.read_text(encoding="utf-8")
    assert list(tmp_path.glob("meiva-prestage.*")) == []

    # meiva:note
    # A failed `pnpm fetch` also aborts prestage non-zero before harness convergence runs.
    (rig.repo / "ui" / "web" / "node_modules").mkdir()
    rig.log_file.write_text("", encoding="utf-8")
    res_pnpm_fail = rig.run(rig.pnpm_sha, FAKE_UV_RC="0", FAKE_PNPM_RC="1")
    assert res_pnpm_fail.returncode == 1
    calls = rig.log_file.read_text(encoding="utf-8").splitlines()
    assert "uv:timeout=120:pyproject=yes" in calls
    assert any(line.startswith("pnpm:fetch") for line in calls)
    assert not any(line.startswith("python:") for line in calls)


def test_the_gateway_programs_are_asked_by_verb() -> None:
    assert roster_command().endswith('"$S/.venv/bin/python" - roster')
    assert refresh_command().endswith('meiva" packages refresh')
    assert drift_command("plugins verify").endswith('meiva" plugins verify')


def test_the_machine_name_is_what_the_host_itself_boots_into() -> None:
    command = machine_name_command()
    assert "ConfiguredMachineIdentity(ConfigBoot()).name()" in command
    assert command.count("python") == 1 and command.endswith('.name())"')


def test_the_pre_update_check_quotes_both_programs_into_the_gateways_script() -> None:
    template = "{home}\nread: {read_rows}\ncheck: {check_rows}\nnew: {new}\nage: {max_age_min}"
    program = "print('a b')\n"
    script = pre_verify_script(
        template, new=_NEW, read_rows=program, check_rows="x", max_age_min=90
    )
    assert script == f"{HOME}\nread: {shlex.quote(program)}\ncheck: x\nnew: {_NEW}\nage: 90"


def test_a_linux_host_starts_over_ssh_and_a_macos_host_through_a_one_shot_job() -> None:
    plain = start_script(macos=False, alias="lin", stamp="S", timeout_s=60, flags="--all-services")
    assert plain == start_command("--all-services")
    job = start_script(macos=True, alias="mac", stamp="S", timeout_s=60, flags="--all-services")
    assert 'launchctl bootstrap "gui/$(id -u)"' in job and "ONESHOT_RC" in job
    assert "com.meiva.fleet-update.mac.S" in job


def test_the_one_shot_waits_the_start_timeout_in_half_second_polls() -> None:
    job = start_script(macos=True, alias="mac", stamp="S", timeout_s=1800, flags="--all-services")
    assert '[ "$n" -ge 3600 ]' in job and "sleep 0.5" in job


def test_an_alias_is_made_safe_for_the_job_label() -> None:
    job = start_script(macos=True, alias="mac mini/1", stamp="S", timeout_s=60, flags="x")
    assert "com.meiva.fleet-update.mac-mini-1.S" in job


@pytest.mark.parametrize(
    ("line", "kept", "gone"),
    [
        ("MEIVA_CLUSTER_SECRET=s3cret-a", "MEIVA_CLUSTER_SECRET=<redacted>", "s3cret-a"),
        ("Authorization: Bearer tok-b", "Bearer <redacted>", "tok-b"),
        ("postgresql://u:pw-c@h/db", "postgresql://u:<redacted>@h/db", "pw-c"),
        ('{"api_key": "k-d"}', '"api_key": "<redacted>"', "k-d"),
        ("capability_key: ck-e, next", "capability_key: <redacted>, next", "ck-e"),
        ("db_password='p-f'", "db_password='<redacted>'", "p-f"),
    ],
)
def test_a_credential_in_a_line_is_replaced_and_the_rest_of_the_line_is_kept(
    line: str, kept: str, gone: str
) -> None:
    out = redact(line)
    assert kept in out and gone not in out


def test_a_line_without_a_credential_is_left_alone() -> None:
    line = 'head=0123 op={"operation": "stop"} https://example.invalid/path'
    assert redact(line) == line

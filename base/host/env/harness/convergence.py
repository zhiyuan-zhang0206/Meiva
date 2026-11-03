# meiva:note
# Local external harness CLI convergence: verifies and aligns installed harness
# CLIs with this checkout's pinned versions without interrupting running agent
# processes.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from base.host.env.harness.catalog import (
    CATALOG,
    ConvergenceReport,
    HarnessSpec,
    HarnessUpdateOutcome,
    OutcomeStatus,
    UpdateSource,
    install_command,
    needs_cli_update,
    parse_cli_version,
    staged_install_command,
)
from base.host.env.harness_binaries import locate_harness_binary, with_harness_path
from base.native_process.os_platform import file_lock

__all__ = [
    "CommandResult",
    "ConvergenceReport",
    "HarnessUpdateOutcome",
    "OutcomeStatus",
    "ShellRunner",
    "UpdateSource",
    "converge_local_harness_clis",
]


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ShellRunner(Protocol):
    def __call__(
        self, command: str, *, timeout: float, env: Mapping[str, str]
    ) -> CommandResult: ...


_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def _redact(text: str) -> str:
    return _SECRET_PATTERN.sub("sk-***", text)


def _append_install_log(
    log_file: Path,
    *,
    now: datetime,
    harness: HarnessSpec,
    previous_version: str,
    installed_version: str,
    source: UpdateSource,
) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": now.isoformat(timespec="seconds"),
        "harness": harness.cli,
        "previous": previous_version,
        "requested": harness.pinned_version,
        "installed": installed_version,
        "method": f"{harness.installer.kind}:{harness.installer.target}",
        "source": source,
    }
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _backup_binary(
    found_path: Path, backup_dir: Path, cli: str, current_version: str
) -> Path | None:
    if not (found_path.is_file() and not found_path.is_symlink()):
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{cli}-{current_version or 'unknown'}.bak"
    shutil.copy2(found_path, backup_path)
    return backup_path


def _verify_staged_binary(
    *,
    harness: HarnessSpec,
    stage_dir: str,
    exec_env: Mapping[str, str],
    run_shell: ShellRunner,
) -> tuple[str, str | None]:
    cmd = staged_install_command(harness, stage_dir)
    stage_res = run_shell(cmd, timeout=600.0, env=exec_env)
    if not stage_res.ok:
        return "", stage_res.stderr or "staging command failed"

    staged_bin = Path(stage_dir) / harness.binary
    if not staged_bin.is_file():
        return "", f"staged binary {staged_bin} was not created"
    staged_bin.chmod(0o755)

    verify_res = run_shell(f"{shlex.quote(str(staged_bin))} --version", timeout=30.0, env=exec_env)
    if not verify_res.ok:
        return "", verify_res.stderr or "staged verification failed"

    installed_ver = parse_cli_version(f"{verify_res.stdout}\n{verify_res.stderr}")
    if not installed_ver or needs_cli_update(harness, installed_ver):
        return (
            installed_ver,
            f"staged verification failed: got {installed_ver!r}, wanted >= {harness.pinned_version!r}",
        )

    return installed_ver, None


def _update_unpinned_staged(
    *,
    harness: HarnessSpec,
    found: str,
    current_version: str,
    exec_env: Mapping[str, str],
    run_shell: ShellRunner,
    backup_dir: Path,
    log_file: Path,
    now: datetime,
    source: UpdateSource,
) -> HarnessUpdateOutcome:
    found_path = Path(found)
    stage_parent = found_path.parent
    try:
        with tempfile.TemporaryDirectory(
            dir=stage_parent, prefix=f".{harness.cli}-stage-"
        ) as stage_dir:
            installed_ver, err = _verify_staged_binary(
                harness=harness, stage_dir=stage_dir, exec_env=exec_env, run_shell=run_shell
            )
            if err is not None:
                return HarnessUpdateOutcome(
                    cli=harness.cli,
                    status="failed",
                    previous_version=current_version,
                    target_version=harness.pinned_version,
                    binary_path=found,
                    detail=_redact(err.strip()),
                )

            _backup_binary(found_path, backup_dir, harness.cli, current_version)
            staged_bin = Path(stage_dir) / harness.binary
            staged_bin.replace(found)

            _append_install_log(
                log_file,
                now=now,
                harness=harness,
                previous_version=current_version,
                installed_version=installed_ver,
                source=source,
            )
            return HarnessUpdateOutcome(
                cli=harness.cli,
                status="updated",
                previous_version=current_version,
                target_version=harness.pinned_version,
                installed_version=installed_ver,
                binary_path=found,
            )
    except Exception as exc:
        return HarnessUpdateOutcome(
            cli=harness.cli,
            status="failed",
            previous_version=current_version,
            target_version=harness.pinned_version,
            binary_path=found,
            detail=_redact(f"staging failed: {exc}"),
        )


def _npm_prefix_override(
    harness: HarnessSpec, user_home: Path, exec_env: Mapping[str, str], run_shell: ShellRunner
) -> str:
    if harness.installer.kind != "npm":
        return ""
    root_res = run_shell("npm root -g", timeout=30.0, env=exec_env)
    root_dir = root_res.stdout.strip() if root_res.ok else ""
    if not root_dir or not os.access(root_dir, os.W_OK):
        return shlex.quote(str(user_home / ".local"))
    return ""


def _rollback_pinned(
    *,
    harness: HarnessSpec,
    found: str,
    current_version: str,
    backup_path: Path | None,
    prefix: str,
    exec_env: Mapping[str, str],
    run_shell: ShellRunner,
) -> str:
    if backup_path is not None and backup_path.is_file():
        try:
            backup_path.replace(found)
            return " (restored previous binary from backup)"
        except Exception as rb_exc:
            return f" (backup restore failed: {rb_exc})"
    if current_version:
        rb_cmd = install_command(harness, current_version, prefix=prefix)
        rb_res = run_shell(rb_cmd, timeout=600.0, env=exec_env)
        return (
            f" (reinstalled previous version {current_version}: {'ok' if rb_res.ok else 'failed'})"
        )
    return ""


def _update_pinned(
    *,
    harness: HarnessSpec,
    found: str,
    current_version: str,
    user_home: Path,
    search_path: str | None,
    exec_env: Mapping[str, str],
    run_shell: ShellRunner,
    backup_dir: Path,
    log_file: Path,
    now: datetime,
    source: UpdateSource,
) -> HarnessUpdateOutcome:
    found_path = Path(found)
    prefix = _npm_prefix_override(harness, user_home, exec_env, run_shell)
    backup_path = _backup_binary(found_path, backup_dir, harness.cli, current_version)

    cmd = install_command(harness, harness.pinned_version, prefix=prefix)
    install_res = run_shell(cmd, timeout=600.0, env=exec_env)

    new_found, _ = locate_harness_binary(harness.binary, home=user_home, path_env=search_path)
    verify_path = new_found if new_found is not None else found
    verify_res = run_shell(f"{shlex.quote(verify_path)} --version", timeout=30.0, env=exec_env)
    installed_ver = (
        parse_cli_version(f"{verify_res.stdout}\n{verify_res.stderr}") if verify_res.ok else ""
    )

    if install_res.ok and verify_res.ok and installed_ver == harness.pinned_version:
        _append_install_log(
            log_file,
            now=now,
            harness=harness,
            previous_version=current_version,
            installed_version=installed_ver,
            source=source,
        )
        return HarnessUpdateOutcome(
            cli=harness.cli,
            status="updated",
            previous_version=current_version,
            target_version=harness.pinned_version,
            installed_version=installed_ver,
            binary_path=verify_path,
        )

    rollback_note = _rollback_pinned(
        harness=harness,
        found=found,
        current_version=current_version,
        backup_path=backup_path,
        prefix=prefix,
        exec_env=exec_env,
        run_shell=run_shell,
    )
    err_snippet = (
        (install_res.stderr if not install_res.ok else "")
        or (verify_res.stderr if not verify_res.ok else "")
        or f"version mismatch: got {installed_ver!r}, wanted {harness.pinned_version!r}"
    )
    return HarnessUpdateOutcome(
        cli=harness.cli,
        status="failed",
        previous_version=current_version,
        target_version=harness.pinned_version,
        binary_path=found,
        detail=_redact(f"{err_snippet.strip()}{rollback_note}"),
    )


def converge_local_harness_clis(
    *,
    user_home: Path,
    meiva_home: Path,
    now: datetime,
    source: UpdateSource,
    base_env: Mapping[str, str],
    run_shell: ShellRunner,
    harnesses: Sequence[HarnessSpec] | None = None,
) -> ConvergenceReport:
    lock_path = meiva_home / "run" / "harness-cli-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(lock_path, timeout_s=120.0):
        exec_env = with_harness_path(base_env, home=user_home)
        search_path = base_env.get("PATH", "")
        targets = tuple(CATALOG.values()) if harnesses is None else harnesses
        outcomes: list[HarnessUpdateOutcome] = []
        backup_dir = meiva_home / "backups" / "harness-cli"
        log_file = meiva_home / "onboarding" / "install-log.jsonl"

        for harness in targets:
            found, _ = locate_harness_binary(harness.binary, home=user_home, path_env=search_path)
            if found is None:
                outcomes.append(
                    HarnessUpdateOutcome(
                        cli=harness.cli,
                        status="skipped_absent",
                        target_version=harness.pinned_version,
                    )
                )
                continue

            ver_res = run_shell(f"{shlex.quote(found)} --version", timeout=30.0, env=exec_env)
            current_version = (
                parse_cli_version(f"{ver_res.stdout}\n{ver_res.stderr}") if ver_res.ok else ""
            )

            if not needs_cli_update(harness, current_version):
                outcomes.append(
                    HarnessUpdateOutcome(
                        cli=harness.cli,
                        status="up_to_date",
                        previous_version=current_version,
                        target_version=harness.pinned_version,
                        installed_version=current_version,
                        binary_path=found,
                    )
                )
                continue

            needed = "npm" if harness.needs_node else "curl"
            if shutil.which(needed, path=exec_env.get("PATH")) is None:
                outcomes.append(
                    HarnessUpdateOutcome(
                        cli=harness.cli,
                        status="failed",
                        previous_version=current_version,
                        target_version=harness.pinned_version,
                        binary_path=found,
                        detail=f"missing prerequisite {needed}",
                    )
                )
                continue

            if not harness.installer.pins:
                outcome = _update_unpinned_staged(
                    harness=harness,
                    found=found,
                    current_version=current_version,
                    exec_env=exec_env,
                    run_shell=run_shell,
                    backup_dir=backup_dir,
                    log_file=log_file,
                    now=now,
                    source=source,
                )
            else:
                outcome = _update_pinned(
                    harness=harness,
                    found=found,
                    current_version=current_version,
                    user_home=user_home,
                    search_path=search_path,
                    exec_env=exec_env,
                    run_shell=run_shell,
                    backup_dir=backup_dir,
                    log_file=log_file,
                    now=now,
                    source=source,
                )
            outcomes.append(outcome)

        return ConvergenceReport(outcomes=tuple(outcomes))

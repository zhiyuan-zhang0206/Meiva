"""Daily OS job for the PR-flow sampler (task #2139).

One daily job hands `scripts/ci/pull_requests/pr_flow_export.py` to the platform scheduler
(launchd / crontab) at 00:25 host-local time: the previous cluster-tz day is
complete by then, so the run aggregates it — and re-emits the whole trailing
window — with the day's data final.

The job is **credential-gated**: it registers only on a unit whose machine
holds the sampler's two credentials — a `gh` session on PATH and
`~/.trunk/api-token` — and only on the registered production home. In the
fleet that is macmini, the location the 2026-09-14 design verified; every
other unit's converge skips quietly, and a machine that later gains the
credentials registers on its own next converge. Nothing here probes the
network: the checks are `shutil.which` plus one file read, so converge
stays cheap.

The job command runs the checkout's own venv python against the checkout's
`scripts/ci/pull_requests/pr_flow_export.py` (same checkout rule as
`base.host.system.cron.meiva_binary_path`), so a worktree's converge — should the gate
ever pass there — would register its own pair, never prod's.

"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

import base.host.system.cron
from base.host.system import pr_flow_content as content
from base.host.system.hostport import Host
from base.host.system.job_readings import read_launchd_path, read_meiva_binary
from base.host.system.job_values import JobPaths, JobRoots, plist_path


def launchd_plist_path() -> Path:
    return plist_path(content.LABEL, user_home=Path.home())


def register_macos(*, roots: JobRoots, host: Host) -> int:
    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))
    path_env = read_launchd_path(paths.meiva, user_home=roots.user_home, host=host)
    label = content.LABEL
    destination = plist_path(content.LABEL, user_home=roots.user_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_file = content.log_path(roots)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        content.launchd_plist_content(paths=paths, path_env=path_env), encoding="utf-8"
    )

    if base.host.system.cron.reload_launchd_job(label, destination, host=host) != 0:
        return 1
    logger.info(
        "launchd job '{}' loaded (daily at {:02d}:{:02d})", label, content.HOUR, content.MINUTE
    )
    return 0


def unregister_macos(*, host: Host) -> int:
    retired = base.host.system.cron.remove_launchd_job(
        content.LABEL, launchd_plist_path(), host=host
    )
    return 0 if retired else 1


def register_linux(*, roots: JobRoots, host: Host) -> int:
    missing_rc = base.host.system.cron.require_crontab(
        "  * PR flow: crontab not installed; the daily sampler job cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    paths = JobPaths(roots, read_meiva_binary(roots.repo, host=host))

    marker = content.MARKER
    log_file = content.log_path(roots)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if base.host.system.cron.replace_crontab_entry(
        marker,
        content.cron_entry(paths),
        skip_phrase="PR-flow registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        host=host,
    ):
        return 1
    logger.info("crontab PR-flow entry added ({})", marker)
    return 0


def unregister_linux(*, host: Host) -> int:
    return base.host.system.cron.remove_crontab_entry(
        content.MARKER, write_failure_rc=1, on_removed=None, host=host
    )


def credential_blocker(*, production: bool, roots: JobRoots, host: Host) -> str | None:
    if not production:
        return "not the registered production home"
    if host.which("gh") is None:
        return "gh CLI not on PATH"
    token = content.trunk_token_path(roots)
    if not token.exists() or not token.read_text(encoding="utf-8").strip():
        return f"no Trunk API token at {token}"
    return None


def register_pr_flow_job(*, enabled: bool, production: bool, roots: JobRoots, host: Host) -> None:
    if not enabled:
        base.host.system.cron.skip_os_job("pr flow")
        return
    if not base.host.system.cron.owns_os_job_home("pr flow", home=roots.home):
        return
    blocker = credential_blocker(production=production, roots=roots, host=host)
    if blocker is not None:
        logger.info("PR-flow sampler job not registered: {}", blocker)
        return
    base.host.system.cron.register_platform_job(
        "PR-flow",
        macos=lambda: register_macos(roots=roots, host=host),
        linux=lambda: register_linux(roots=roots, host=host),
        host=host,
    )


def unregister_pr_flow_job(*, host: Host) -> None:
    if not base.host.system.cron.owns_os_jobs("pr flow"):
        return
    base.host.system.cron.unregister_platform_job(
        macos=lambda: unregister_macos(host=host),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )

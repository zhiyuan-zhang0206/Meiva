# meiva:link base/host/docs/health-probe-job.meiva.okf.md

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from base.host.system.cron import health_content, mechanics
from base.host.system.hostport import Host
from base.native_process.os_platform import descends_from_launchd_job, launchd_job_label
from base.paths import meiva_home

DEFAULT_INTERVAL_SECONDS = 300
_CRON_MARKER = "# meiva-health-probe"


def _launchd_plist_path(user_home: Path | None = None) -> Path:
    # meiva:note
    # Path to the health probe's launchd plist.
    return mechanics.user_plist_path(health_content.LABEL, user_home=user_home)


def _own_probe_job_of(
    own_labels: set[str],
    *,
    uid: int,
    current_label: str | None = None,
    ancestral_labels: frozenset[str] | None = None,
) -> str | None:
    # meiva:note
    # The health-probe label this process runs under, or None when external.
    #
    # ``XPC_SERVICE_NAME`` matches only for the job's direct child — exec'd
    # descendants read "0" (see `base.native_process.os_platform.descends_from_launchd_job`) —
    # so the environment is a fast path and the live process tree is the proof.
    # Labels are probed in sorted order for deterministic logging/reporting.
    current = launchd_job_label() if current_label is None else current_label
    if current is not None and current in own_labels:
        return current
    if ancestral_labels is not None:
        return health_content.own_probe_job_of(
            own_labels,
            current_label=current,
            ancestral_labels=ancestral_labels,
        )
    for candidate in sorted(own_labels):
        if descends_from_launchd_job(candidate, uid=uid):
            return candidate
    return None


def register_macos(
    interval_s: int,
    *,
    host: Host,
    user_home: Path | None = None,
    current_label: str | None = None,
    ancestral_labels: frozenset[str] | None = None,
) -> int:
    # meiva:note
    # Register the health probe as a launchd User LaunchAgent.
    #
    # Writes the plist to ~/Library/LaunchAgents/ and loads it with
    # `launchctl bootstrap`. A call descended from this job leaves its own loaded
    # spec untouched; the next external converge applies any pending change.
    label = health_content.LABEL
    plist_path = _launchd_plist_path(user_home)

    # meiva:note
    # `bootout` terminates the job's whole process tree. A registering job
    # cannot replace itself safely. Leave its plist in place so the next
    # external converge sees the desired-content change and reloads it.
    # Ownership is two-pronged: the inherited `XPC_SERVICE_NAME` is a cheap
    # fast path that only the job's direct child matches, while descendants —
    # where converges actually run — read "0", so the live process tree check
    # is the proof; see the linked health-job custody contract.
    own_job = _own_probe_job_of(
        {label},
        uid=host.uid,
        current_label=current_label,
        ancestral_labels=ancestral_labels,
    )
    if own_job is not None:
        logger.info("Health probe '{}' is registering itself — deferring reload", own_job)
        return 0

    # meiva:note
    # Ensure the LaunchAgents directory exists.
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # meiva:note
    # Write the plist.
    meiva_path = mechanics.meiva_binary_path(host=host)
    log_file = meiva_home() / "logs" / "health-probe.log"
    environment_home = Path(mechanics.job_home())
    path_env = mechanics.launchd_path_env(host=host, user_home=user_home)
    content = health_content.launchd_plist_content(
        arguments=(meiva_path, "cluster", "health-probe"),
        interval_seconds=interval_s,
        log_file=log_file,
        environment_home=environment_home,
        path_env=path_env,
    )
    plist_path.write_text(content)
    logger.info("Wrote plist to {}", plist_path)

    # meiva:note
    # Unload any existing instance (ignore errors if not loaded).
    host.run(
        ["launchctl", "bootout", f"gui/{host.uid}/{label}"],
        capture_output=True,
        timeout=mechanics.TOOL_TIMEOUT_S,
    )

    # meiva:note
    # Load (bootstrap) the new plist.
    result = host.run(
        ["launchctl", "bootstrap", f"gui/{host.uid}", str(plist_path)],
        capture_output=True,
        text=True,
        timeout=mechanics.TOOL_TIMEOUT_S,
    )
    if result.returncode != 0:
        logger.error("launchctl bootstrap failed: {}", result.stderr)
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, interval_s)
    return 0


def unregister_macos(*, host: Host, user_home: Path | None = None) -> int:
    # meiva:note
    # Remove the health probe's launchd job and plist.
    plist_path = _launchd_plist_path(user_home)
    label = health_content.LABEL

    # meiva:note
    # Bootout the job.
    retired = mechanics.bootout_launchd_job(label, host=host)

    # meiva:note
    # Remove the plist.
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed plist {}", plist_path)

    if not retired:
        return 1
    logger.info("launchd job '{}' unloaded", label)
    return 0


def register_linux(interval_s: int, *, host: Host, repo: Path | None = None) -> int:
    # meiva:note
    # Add the health probe to the user's crontab.
    #
    # Reads the current crontab, removes any existing Meiva health-probe entry,
    # appends the new one, and writes it back. Idempotent. The crontab line runs
    # the observation-only probe directly, with no shell wrapper.
    #
    # When crontab is not installed on the host (a minimal Linux box such as a
    # hermetic bench / CI container), the health probe is a capability this host
    # cannot provide — warn and skip rather than fail the whole bring-up, the same
    # way converge degrades on an absent browser / permissions-helper. A real long-lived
    # gateway has cron and registers normally; an actual registration error (cron
    # present but the write fails) still returns non-zero below.
    missing = mechanics.require_crontab(
        "  ! health probe cron: crontab not installed on this host (skipping); "
        "cluster runs without a health-probe cron",
        missing_returncode=0,
        missing_stream=sys.stdout,
        host=host,
    )
    if missing is not None:
        return missing

    meiva_path = mechanics.meiva_binary_path(host=host, repo=repo)
    minutes = max(1, interval_s // 60)
    entry = f"*/{minutes} * * * * {mechanics.cron_env_prefix()}{meiva_path} cluster health-probe {_CRON_MARKER}"

    def report_update_failure(err: str) -> None:
        # meiva:note
        # A failed crontab write remains a CLI diagnostic on stderr.
        print(f"  * crontab update failed: {err}", file=sys.stderr)  # noqa: T201

    rc = mechanics.replace_crontab_entry(
        _CRON_MARKER,
        entry,
        skip_phrase="health-probe registration",
        update_failure=report_update_failure,
        host=host,
    )
    if rc == 0:
        print(f"  . crontab entry added (every {minutes} min)")  # noqa: T201
    return rc


def unregister_linux(*, host: Host) -> int:
    # meiva:note
    # Remove the health-probe entry from the user's crontab.

    def report_read_failure() -> None:
        # meiva:note
        # An unreadable or missing table retains the existing absence message.
        print("  . no crontab to unregister")  # noqa: T201

    def report_absent() -> None:
        # meiva:note
        # A missing health entry is reported without claiming another job was removed.
        print("  . no Meiva health-probe entry found in crontab")  # noqa: T201

    def report_removed() -> None:
        # meiva:note
        # Only a successful write reports that the health entry was removed.
        print("  . crontab entry removed")  # noqa: T201

    return mechanics.remove_crontab_entry(
        _CRON_MARKER,
        write_failure_rc=0,
        on_removed=report_removed,
        on_read_failed=report_read_failure,
        on_absent=report_absent,
        host=host,
    )


# meiva:note
# Register the OS cron job for the cluster health probe.
#
# Platform-aware: dispatches to the macOS or Linux registrar.
# Idempotent — re-running updates the interval and reloads the job.
# A no-op when ``enabled`` is off (the test suite) or when this
# process's home is not the default home (``owns_os_jobs``).
#
# Refused (with an error log) when this process runs from a non-prod
# checkout against the prod home — the 2026-08-07 accident (Task #1025): a
# debug/test process launched from a worktree's venv (``from gateway.app
# import app`` + TestClient, no conftest) invoked this registrar, which
# rewrote the prod health-probe plist to point at the worktree's disposable
# ``meiva``. The probe then ran worktree code against prod data, misjudged
# schema health, and auto-rolled-back (quiesce restarted every agent). The
# job's content is anchored to THIS process's checkout, so the registration
# must be too: same rule as ``meiva start`` (``prod_service_checkout_error``).
#
# Raises:
#     RuntimeError: on registration failure.
def register_os_cron(
    interval_s: int = DEFAULT_INTERVAL_SECONDS,
    *,
    enabled: bool,
    host: Host,
    home: Path | None = None,
    repo: Path | None = None,
    user_home: Path | None = None,
    ancestral_labels: frozenset[str] | None = None,
) -> None:
    if not enabled:
        mechanics.skip_os_job("health-probe")
        return
    owns = (
        mechanics.owns_os_jobs("health-probe")
        if home is None
        else mechanics.owns_os_job_home("health-probe", home=home)
    )
    if not owns:
        return
    from base.paths import prod_service_checkout_error, repo_root

    checkout = repo_root() if repo is None else repo
    refusal = prod_service_checkout_error(checkout)
    if refusal is not None:
        logger.error("health-probe registration refused: {}", refusal)
        return
    mechanics.register_platform_job(
        "cron",
        macos=lambda: register_macos(
            interval_s,
            host=host,
            user_home=user_home,
            ancestral_labels=ancestral_labels,
        ),
        linux=lambda: register_linux(interval_s, host=host, repo=repo),
        host=host,
    )


def unregister_os_cron(*, host: Host, user_home: Path | None = None) -> None:
    # meiva:note
    # Remove the OS cron job of the cluster health probe.
    #
    # Platform-aware. Safe to call when no job is registered (no-op), and a no-op
    # when this process's home is not the default home (``owns_os_jobs``).
    if not mechanics.owns_os_jobs("health-probe"):
        return
    mechanics.unregister_platform_job(
        macos=lambda: unregister_macos(host=host, user_home=user_home),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )

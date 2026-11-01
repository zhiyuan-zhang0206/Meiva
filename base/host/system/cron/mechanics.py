# meiva:link base/host/docs/health-probe-job.meiva.okf.md

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from loguru import logger

from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots
from base.native_process.os_platform import crontab_lock
from base.paths.home import is_default_home_of

# meiva:note
# The bound only keeps a wedged scheduler command from stalling its caller.
TOOL_TIMEOUT_S = 30.0


# meiva:note
# Why registration is gated on `general.os_jobs_enabled`.
#
# Every other host resource a test isolates — `$MEIVA_HOME`, the database, redis, the service
# ports, the session records — is addressed by a value the process reads, so redirecting the
# value redirects the resource. The platform scheduler is not: launchd reads ONE
# `~/Library/LaunchAgents` per OS user, `crontab` edits ONE table per user. A test-scoped home
# therefore isolates everything about a registered job except the namespace it lands in.
#
# So the suite turns registration off wholesale (`MEIVA_OS_JOBS_ENABLED=false`, set in
# `tests/fixtures/env_bootstrap.py` and inherited by every subprocess it spawns) instead of
# registering into the operator's namespace and unregistering afterwards: a job that is never
# armed cannot fire mid-run, and cannot survive a session that is SIGKILLed before its teardown.
# Each `register_*` takes the switch as an `enabled` value from its composition root.
#
# Deregistration is deliberately NOT gated — a cleanup path has to work wherever registration is
# forbidden.


# meiva:note
# Log that `kind` was not registered because OS jobs are disabled.
def skip_os_job(kind: str) -> None:
    logger.info("OS jobs disabled (MEIVA_OS_JOBS_ENABLED=false) — not registering {}", kind)


# meiva:note
# Whether this account's default home may register or remove the `kind` OS job.
#
# Labels and crontab markers carry no home, so the scheduler's one namespace per
# OS user is shared by every process on the host, and only the default home
# (`~/.meiva`) may write or remove an entry in it. A scratch home (a test, a tool,
# a disposable cluster) that reached a registrar would otherwise replace or
# `bootout` the host's real job. Both directions are gated, removal included,
# unlike the `enabled` switch, which gates registration only.
def owns_os_job_home(kind: str, *, roots: JobRoots) -> bool:
    if is_default_home_of(roots.home, user_home=roots.user_home):
        return True
    logger.info("{} is not the default home: leaving the {} OS job alone", roots.home, kind)
    return False


def reload_launchd_job(label: str, plist_path: Path, *, host: Host) -> int:
    # meiva:note
    # Replace a loaded LaunchAgent after its caller writes the plist.
    host.run(
        ["launchctl", "bootout", f"gui/{host.uid}/{label}"],
        capture_output=True,
        timeout=TOOL_TIMEOUT_S,
    )
    result = host.run(
        ["launchctl", "bootstrap", f"gui/{host.uid}", str(plist_path)],
        capture_output=True,
        text=True,
        timeout=TOOL_TIMEOUT_S,
    )
    if result.returncode != 0:
        logger.error("launchctl bootstrap failed for {}: {}", label, result.stderr)
        return 1
    return 0


# meiva:note
# `launchctl bootout` exits non-zero both when it removed the job and when none was loaded, so
# its status cannot answer "is it gone"; `launchctl print` can (exit 0 = launchd still holds
# it, any non-zero = absence). A job that survives its bootout keeps running the code it was
# loaded with, so an unproven retirement returns False and reports rather than claiming success.
# meiva:link base/host/docs/host.meiva.okf.md
def bootout_launchd_job(label: str, *, host: Host) -> bool:
    host.run(
        ["launchctl", "bootout", f"gui/{host.uid}/{label}"],
        capture_output=True,
        timeout=TOOL_TIMEOUT_S,
    )
    probe = host.run(
        ["launchctl", "print", f"gui/{host.uid}/{label}"],
        capture_output=True,
        timeout=TOOL_TIMEOUT_S,
    )
    if probe.returncode == 0:
        logger.error(
            "launchctl still holds {label} after bootout; it keeps running the code it was "
            "loaded with and its plist was removed under it",
            label=label,
            event="launchd_retirement",
        )
        return False
    return True


def remove_launchd_job(label: str, plist_path: Path, *, host: Host) -> bool:
    retired = bootout_launchd_job(label, host=host)
    plist_path.unlink(missing_ok=True)
    return retired


def require_crontab(
    missing_message: str, *, missing_returncode: int, missing_stream: TextIO, host: Host
) -> int | None:
    # meiva:note
    # Report a missing crontab with the caller's existing message and policy.
    if host.which("crontab") is None:
        print(missing_message, file=missing_stream)
        return missing_returncode
    return None


# meiva:note
# One rule for which lines this entry owns: the caller's predicate when it has one, the marker
# substring otherwise. Rewriting and removing ask it, so a line cannot be kept by one and replaced
# by the other.
def _owns(line: str, marker: str, owns_line: Callable[[str], bool] | None) -> bool:
    return owns_line(line) if owns_line is not None else marker in line


# meiva:note
# The canonical entry, plus whatever an operator wrote after the marker on the line it replaces.
#
# `(restored 2026-10-30 by #7100)` and its kind are hand-written and are the only record of which
# line was re-added and by whom; the canonical rewrite replaced the whole line and wiped them
# (reported by 7100 on 2026-10-31). The command belongs to the caller's canonical entry, the note
# does not -- it is carried over verbatim. Nothing is invented: owned lines that disagree about the
# note, or carry none, leave the entry as the caller wrote it.
def _with_carried_note(entry: str, owned: Sequence[str], marker: str) -> str:
    tails = {line.partition(marker)[2].strip() for line in owned if marker in line}
    tails.discard("")
    return f"{entry} {tails.pop()}" if len(tails) == 1 else entry


def replace_crontab_entry(
    marker: str,
    entry: str,
    *,
    skip_phrase: str,
    update_failure: Callable[[str], None],
    owns_line: Callable[[str], bool] | None = None,
    user_home: Path,
    host: Host,
) -> int:
    # meiva:note
    # Replace owned lines, defaulting to a marker substring match.
    #
    # The caller reports a failed write in its own channel.
    with crontab_lock(user_home=user_home):
        result = host.run(["crontab", "-l"], capture_output=True, text=True, timeout=TOOL_TIMEOUT_S)
        # meiva:note
        # Only benign "no crontab" means empty; other read failures must block a clobbering rewrite.
        if result.returncode != 0 and "no crontab" not in (result.stderr or "").lower():
            print(  # noqa: T201
                f"  * crontab -l failed ({result.stderr.strip() or result.returncode}); "
                f"skipping {skip_phrase} to avoid clobbering the crontab",
                file=sys.stderr,
            )
            return 1
        current = result.stdout if result.returncode == 0 else ""
        present = current.splitlines()
        owned = [line for line in present if _owns(line, marker, owns_line)]
        lines = [line for line in present if not _owns(line, marker, owns_line)]
        lines.append(_with_carried_note(entry, owned, marker))
        result = host.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_S,
        )
        if result.returncode != 0:
            update_failure(result.stderr)
            return 1
    return 0


def remove_crontab_entry(
    marker: str,
    *,
    write_failure_rc: int,
    on_removed: Callable[[], None] | None,
    owns_line: Callable[[str], bool] | None = None,
    on_read_failed: Callable[[], None] | None = None,
    on_absent: Callable[[], None] | None = None,
    user_home: Path,
    host: Host,
) -> int:
    # meiva:note
    # Remove owned lines, defaulting to a marker substring match.
    #
    # Read failure and absent callbacks run before their no-op return. Removal is
    # reported only after a successful write.
    with crontab_lock(user_home=user_home):
        result = host.run(["crontab", "-l"], capture_output=True, text=True, timeout=TOOL_TIMEOUT_S)
        if result.returncode != 0:
            if on_read_failed is not None:
                on_read_failed()
            return 0
        present = result.stdout.splitlines()
        lines = [line for line in present if not _owns(line, marker, owns_line)]
        if len(lines) == len(present):
            if on_absent is not None:
                on_absent()
            return 0
        result = host.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_S,
        )
        if result.returncode != 0:
            return write_failure_rc
        if on_removed is not None:
            on_removed()
    return 0


def register_platform_job(
    what: str, *, macos: Callable[[], int], linux: Callable[[], int], host: Host
) -> None:
    # meiva:note
    # Run this host's registrar for an OS job; a non-zero return code is a failed registration.
    #
    # Raises:
    #     RuntimeError: on an unsupported platform or a failed registration.
    if host.platform.system == "darwin":
        rc, platform = macos(), "macOS"
    elif host.platform.system == "linux":
        rc, platform = linux(), "Linux"
    else:
        raise RuntimeError("unsupported host platform for OS jobs")
    if rc != 0:
        raise RuntimeError(f"{what} registration failed on {platform}")


def unregister_platform_job(
    *, macos: Callable[[], int], linux: Callable[[], int], host: Host
) -> None:
    # meiva:note
    # Run this host's remover for an OS job (the return code is not an error: removal is idempotent).
    if host.platform.system == "darwin":
        macos()
    elif host.platform.system == "linux":
        linux()
    else:
        raise RuntimeError("unsupported host platform for OS jobs")

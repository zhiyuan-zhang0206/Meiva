# meiva:link base/host/docs/health-probe-job.meiva.okf.md

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from loguru import logger

from base.host.system.hostport import Host
from base.host.system.job_readings import read_launchd_path, read_meiva_binary
from base.host.system.job_values import cron_environment, launchd_environment
from base.native_process.os_platform import crontab_lock
from base.paths import meiva_home

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
# Whether this process's home may register or remove the `kind` OS job.
#
# Labels and crontab markers carry no home, so the scheduler's one namespace per
# OS user is shared by every process on the host, and only the default home
# (`~/.meiva`) may write or remove an entry in it. A scratch home (a test, a tool,
# a disposable cluster) that reached a registrar would otherwise replace or
# `bootout` the host's real job. Both directions are gated, removal included,
# unlike the `enabled` switch, which gates registration only.
def owns_os_jobs(kind: str) -> bool:
    from base.paths.home import resolve_meiva_home

    return owns_os_job_home(kind, home=resolve_meiva_home())


# meiva:note
# Protect the scheduler namespace using the command root's captured home.
def owns_os_job_home(kind: str, *, home: Path) -> bool:
    from base.paths.home import is_default_home

    if is_default_home(home):
        return True
    logger.info("{} is not the default home: leaving the {} OS job alone", home, kind)
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


def replace_crontab_entry(
    marker: str,
    entry: str,
    *,
    skip_phrase: str,
    update_failure: Callable[[str], None],
    owns_line: Callable[[str], bool] | None = None,
    host: Host,
) -> int:
    # meiva:note
    # Replace owned lines, defaulting to a marker substring match.
    #
    # The caller reports a failed write in its own channel.
    with crontab_lock():
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
        lines = [
            line
            for line in current.splitlines()
            if not (owns_line(line) if owns_line is not None else marker in line)
        ]
        lines.append(entry)
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
    host: Host,
) -> int:
    # meiva:note
    # Remove owned lines, defaulting to a marker substring match.
    #
    # Read failure and absent callbacks run before their no-op return. Removal is
    # reported only after a successful write.
    with crontab_lock():
        result = host.run(["crontab", "-l"], capture_output=True, text=True, timeout=TOOL_TIMEOUT_S)
        if result.returncode != 0:
            if on_read_failed is not None:
                on_read_failed()
            return 0
        lines = [
            line
            for line in result.stdout.splitlines()
            if not (owns_line(line) if owns_line is not None else marker in line)
        ]
        if len(lines) == len(result.stdout.splitlines()):
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


def meiva_binary_path(*, host: Host, repo: Path | None = None) -> str:
    # meiva:note
    # The absolute path to the `meiva` binary of THIS CHECKOUT.
    #
    # The checkout is the anchor, not PATH. A job spec written by a worktree's code
    # must run that worktree's binary, and the host's global `meiva` on PATH is only a
    # link to whichever checkout last wired it — so resolving the venv directly
    # names the correct file for every checkout, prod included.
    # `shutil.which` survives only as the fallback for an install with no venv (a
    # global pip install).
    #
    # Why the order matters: `which` resolves against the CALLING process's PATH,
    # which under `uv run` / an activated venv is whatever checkout the caller came
    # from — not necessarily the checkout that owns `$MEIVA_HOME`. A process could
    # therefore write a job labelled for its own cluster but pointed at another
    # cluster's binary; when that other binary is prod's, the job runs prod's
    # health probe against prod's home. Resolving from
    # `repo_root()` makes binary, `$MEIVA_HOME` and label come from one checkout.
    from base.paths import repo_root

    checkout = repo_root() if repo is None else repo
    return read_meiva_binary(checkout, host=host)


def job_home() -> str:
    # meiva:note
    # The `$MEIVA_HOME` a generated job spec pins, as a string.
    #
    # Redundancy, not the primary defence: `meiva_binary_path()` already resolves to
    # the checkout that owns this home, and that binary's own boot resolves the
    # same home (`resolve_meiva_home`). Pinning it in the spec makes the job
    # self-describing, and it removes the one path by which a stale job could reach
    # the prod home: an `meiva` that resolved through PATH with no `MEIVA_HOME` set
    # falls back to `~/.meiva`.
    return str(meiva_home())


def launchd_env_block(
    indent: str = "    ", extra: dict[str, str] | None = None, *, host: Host
) -> str:
    # meiva:note
    # The `EnvironmentVariables` plist fragment every LaunchAgent here carries:
    # the cluster's `MEIVA_HOME` (see `job_home`) and a usable PATH (see
    # `launchd_path_env`), followed by whatever `extra` the caller adds.
    #
    # `extra` is rendered in the caller's own order, so one caller's dict always
    # produces the same bytes — a plist that is compared against the one on disk to
    # decide whether the job needs replacing
    # cannot afford a fragment that reshuffles between runs.
    return launchd_environment(
        home=Path(job_home()),
        path_env=launchd_path_env(host=host),
        indent=indent,
        extra=tuple((extra or {}).items()),
    )


def cron_env_prefix() -> str:
    # meiva:note
    # The `MEIVA_HOME=<home> ` prefix every crontab line here carries — the
    # crontab analog of `launchd_env_block` (cron runs each line through /bin/sh,
    # so a leading assignment scopes to that command alone; a bare `MEIVA_HOME=`
    # line would instead apply to the whole file, including a co-located cluster's
    # entries).
    return cron_environment(Path(job_home()))


def launchd_path_env(
    *,
    host: Host,
    meiva_binary: str | None = None,
    user_home: Path | None = None,
) -> str:
    # meiva:note
    # PATH for a LaunchAgent's environment.
    #
    # launchd hands a job a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that omits
    # Homebrew's bin, so an `meiva` subcommand launched from a plist cannot find
    # npx / node. Compose a PATH from the dir holding this host's `meiva`, the brew
    # prefix (probed from the current PATH, falling back to the standard
    # Apple-silicon / Intel locations), and the base system dirs.
    #
    # Shared by every LaunchAgent this repo writes — the autostart job (which runs
    # `meiva start`) and the watchdog probe (which respawns a
    # dead watchdog). A probe without brew on PATH would fail exactly when it is
    # needed, so this is not cosmetic.

    binary = meiva_binary_path(host=host) if meiva_binary is None else meiva_binary
    home = Path.home() if user_home is None else user_home
    return read_launchd_path(binary, user_home=home, host=host)


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

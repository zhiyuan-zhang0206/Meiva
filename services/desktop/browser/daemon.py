"""Shared headed Chrome launcher for the agent-runner.

Launches a real, headed Chrome with a remote-debugging (CDP) port and a
dedicated persistent profile. Same entry pattern as the other meiva daemons
(`python -m services.<name>.daemon`); `os.execvp` makes the supervised session's
process become Chrome. Agents reach the browser over
localhost CDP through the chrome-devtools-mcp plugin (`--browserUrl`).

env overrides (via base.config.settings):
- `MEIVA_BROWSER_ENABLED` — gate (the meiva-browser session only starts when true)
- `MEIVA_CHROME_BINARY` — explicit Chrome path; else the platform default

The CDP port is `settings.services.browser_cdp_port` (default 9222).

The profile lives at `$MEIVA_HOME/chrome-profile/` — dedicated and persistent,
separate from the user's daily Chrome profile (isolated cookie jar; the user
signs it in once). At first `meiva start` the operator may instead seed it from
their daily Chrome (converge's `_ensure_browser` -> `profile.ensure_browser_profile`);
either way, by the time this daemon runs the profile dir is the source of truth
and `mkdir(exist_ok=True)` only backstops the fresh-empty case.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from loguru import logger

from base.config import settings
from base.host.system.hostport import Host
from base.host.system.probes import browser_incapability, resolve_chrome_binary
from base.paths import logs_dir, meiva_home

from . import macos_readiness, session_restore
from . import profile as browser_profile
from .orphan import find_cluster_chrome, reap_cluster_chrome
from .probe import cdp_unreachable, cdp_url, our_chrome_holds_port
from .profile import profile_dir as profile_dir

_CDP_TIMEOUT_S = 2.0
_WATCH_POLL_S = 0.5


def assert_browser_capable(*, host: Host) -> None:
    """Raise RuntimeError with a precise, actionable message if this machine
    cannot host the shared headed browser. Called by the converge preflight and
    main(). The capability check itself lives in base.host.system.probes
    (browser_incapability); this raises its reason so a launch fails loudly with
    the same wording the operator sees in `meiva status`."""
    reason = browser_incapability(settings.services.chrome_binary, host=host)
    if reason is not None:
        raise RuntimeError(f"meiva-browser: {reason}")


def cdp_reachable(port: int) -> bool:
    """True when a debuggable Chrome answers on this CDP port (`/json/version`
    -> 200). Before launch, a served port means a second Chrome on the same
    profile would collide on the singleton profile lock. main() refuses it.
    """
    url = cdp_url(port)
    try:
        with urllib.request.urlopen(url, timeout=_CDP_TIMEOUT_S) as resp:  # noqa: S310 — fixed localhost CDP URL
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


# meiva:note
# The launch line. `--restore-last-session` reopens the profile's last session, and
# only a deliberate `meiva stop` asks for it (through the session ledger):
# relaunching after a crash must not replay the tab set the profile died with.
# meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
def chrome_args(
    binary: str, port: int, profile: Path, *, restore_last_session: bool = False
) -> list[str]:
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if restore_last_session:
        args.append("--restore-last-session")
    return args


# meiva:note
# Launch Chrome in its own session so stopping this keeper unit leaves Chrome running;
# `binary` is the resolved Chrome executable and `args` the internal flags from `chrome_args`.
def spawn_chrome(
    binary: str, args: list[str], *, log_path: Path | None = None
) -> subprocess.Popen[bytes]:
    try:
        if log_path is not None:
            with log_path.open("ab") as log_file:
                return subprocess.Popen(  # noqa: S603
                    args,
                    executable=binary,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                    close_fds=True,
                )
        return subprocess.Popen(  # noqa: S603
            args,
            executable=binary,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        logger.error("meiva-browser spawn failed: {}", exc)
        sys.exit(127)


# meiva:note
# Pure startup decision for the CDP port check: adopt our cluster's healthy Chrome,
# refuse a foreign Chrome holding the port, reap and relaunch when our holder's CDP
# endpoint is wedged, or launch directly when the port is free.
def plan_port_occupancy(
    *, reachable: bool, holder_pid: int | None, cdp_healthy: bool
) -> Literal["launch", "adopt", "refuse", "reap_and_launch"]:
    if not reachable:
        return "launch"
    if holder_pid is None:
        return "refuse"
    if cdp_healthy:
        return "adopt"
    return "reap_and_launch"


# meiva:note
# Pure poll decision for watch_chrome: returns None while Chrome is still alive, 0 when
# the keeper unit received SIGTERM/SIGINT, or a non-zero exit code when Chrome exited.
def watch_step(
    *,
    stopping: bool,
    proc_running: bool,
    proc_returncode: int | None,
    cluster_pids: Sequence[int],
) -> int | None:
    if stopping:
        return 0
    if proc_running or cluster_pids:
        return None
    return proc_returncode if proc_returncode else 1


# meiva:note
# Keep the supervised unit alive while this cluster's Chrome runs. Exits 0 on SIGTERM/SIGINT
# without signalling Chrome so `meiva stop --keep-browser`, `meiva restart --keep-browser` and
# `meiva-root` replacements leave the browser open; exits non-zero when Chrome exits so
# `meiva-root` respawns the unit.
def watch_chrome(
    profile: Path,
    proc: subprocess.Popen[bytes] | None = None,
    *,
    poll_interval_s: float = _WATCH_POLL_S,
) -> None:
    stopping = False

    def _handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    prev_term = signal.signal(signal.SIGTERM, _handle_stop)
    prev_int = signal.signal(signal.SIGINT, _handle_stop)
    try:
        while True:
            proc_running = proc is not None and proc.poll() is None
            proc_rc = proc.returncode if proc is not None else None
            decision = watch_step(
                stopping=stopping,
                proc_running=proc_running,
                proc_returncode=proc_rc,
                cluster_pids=() if proc_running else find_cluster_chrome(profile),
            )
            if decision is None:
                if proc is not None and proc_running:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=poll_interval_s)
                else:
                    time.sleep(poll_interval_s)
                continue
            if decision == 0:
                return
            sys.exit(decision)
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)


def main(*, restore_last_session: bool | None = None) -> None:
    # Fail loud BEFORE the log redirect so the reason is visible in the
    # session log — this also guards healthcheck-triggered respawns, not just the first
    # launch (the converge preflight only runs at `meiva start`).
    host = Host.system()
    assert_browser_capable(host=host)
    cdp_port = settings.services.browser_cdp_port  # the recorded/`.env` port; default 9222
    reachable = cdp_reachable(cdp_port)
    holder = our_chrome_holds_port(cdp_port, profile_dir()) if reachable else None
    healthy = (cdp_unreachable(cdp_port).reason is None) if holder is not None else False
    occupancy = plan_port_occupancy(
        reachable=reachable,
        holder_pid=holder,
        cdp_healthy=healthy,
    )
    if occupancy == "adopt":
        profile = profile_dir()
        logger.info(
            "meiva-browser: adopting running cluster Chrome (pid {}) on CDP port {}",
            holder,
            cdp_port,
        )
        watch_chrome(profile, None)
        return
    if occupancy == "refuse":
        print(  # noqa: T201 — pre-redirect, surfaces in the session log
            f"meiva-browser: CDP port {cdp_port} already served by another Chrome; "
            f"refusing to start a second instance on {profile_dir()}. "
            "To clear it: stop this unit with `meiva stop`, then start it again with `meiva start` — that sweeps any "
            "Chrome running on this cluster's profile, including one left behind by a "
            "SingletonLock handoff. If instead you started a Chrome of your own on this "
            "port, quit that one — the browser service owns this port.",
            file=sys.stderr,
        )
        sys.exit(1)
    if occupancy == "reap_and_launch":
        reap_cluster_chrome()
    # meiva:note
    # Consume a pending restore only here, after the port guard refused nothing:
    # a launch that never happens must not eat the one chance to reopen the tabs.
    # `restore_last_session` is an explicit parameter only so a test can pin the
    # decision; the entry point always reads the ledger.
    if restore_last_session is None:
        restore_last_session = session_restore.begin_launch(meiva_home())
    if restore_last_session:
        logger.info("meiva-browser: restoring the tabs of the last deliberate stop")
    binary = resolve_chrome_binary(settings.services.chrome_binary, host=host)
    if binary is None:  # assert_browser_capable already raised if so — this narrows the type
        sys.exit(127)
    # On macOS a detached process can see a display while lacking the active GUI
    # login session and Keychain context Chrome needs for encrypted profile data.
    # Wait in this supervised process instead of launching an unusable browser;
    # the matching marker lets the healthcheck report degraded state without
    # killing and respawning this deliberate wait.
    macos_readiness.wait_for_browser_startup_readiness()
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    local_state_warning = browser_profile.validate_local_state(profile)
    if local_state_warning is not None:
        logger.warning("meiva-browser: Local State validation warning: {}", local_state_warning)
    proc = spawn_chrome(
        binary,
        chrome_args(binary, cdp_port, profile, restore_last_session=restore_last_session),
        log_path=logs_dir() / "browser.log",
    )
    watch_chrome(profile, proc)


if __name__ == "__main__":
    main()

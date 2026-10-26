# meiva:note
# The browser legs of a stop: record its tabs, wait for Chrome to exit, reap what escaped.

from __future__ import annotations

import signal
import sys

import psutil

from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from cli.commands.data_plane.stop_waits import Deadline, wait_for_exit
from cli.database.runtime import CommandRuntime


# meiva:note
# Write the session ledger a later `meiva start` restores (see session_restore).
# Called before the services phase: that phase is what signals the `meiva-browser`
# session, so waiting for the stop's own `browser` phase would find Chrome already gone.
# meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
def record_browser_session(command: CommandRuntime) -> None:
    from services.desktop.browser import session_restore

    line = session_restore.record_for_stop(
        home=command.home,
        chrome=session_restore.CdpChrome(command.config.view.services.browser_cdp_port),
    )
    if line is not None:
        print(f"  {line}")


def stop_browser(deadline: Deadline) -> None:
    from services.desktop.browser.orphan import find_cluster_chrome

    trees: set[OwnedProcess] = set()
    for pid in find_cluster_chrome():
        try:
            identity = OwnedProcess.capture(psutil.Process(pid))
            retain_processes(trees, capture_tree(identity))
            if identity.live():
                identity.send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            continue
    wait_for_exit(trees, deadline)
    if find_cluster_chrome():
        raise RuntimeError("this unit's browser appeared during stop")


# meiva:note
# Finish a browser teardown the session kill could not: kill any Chrome still
# running on THIS cluster's profile.
#
# Called only on an explicit force teardown with `keep_browser=False`. Normal stop waits for
# Chrome to exit without escalating signals. After a `SingletonLock` handoff Chrome is no
# longer a descendant of the `meiva-browser` session, so killing that session leaves it
# running on the cluster's CDP port and the next launch's port guard refuses;
# `services/desktop/browser/orphan.py` names it by the cluster's own `--user-data-dir` and
# argues why that can never select a Chrome that is not ours.
#
# Silent when there is nothing to reap (the common case): the session kill already reported
# done, so an extra "nothing found" line would only be noise. Never fails the stop: a teardown
# that cannot reach the browser must still tear the rest of the cluster down, and the
# pre-existing manual kill remains the operator's fallback (a decorative degrade, warned).
def reap_cluster_chrome() -> None:
    from services.desktop.browser import orphan

    try:
        pids = orphan.reap_cluster_chrome()
    except Exception as exc:
        print(f"  ⚠ could not sweep this cluster's Chrome: {exc}", file=sys.stderr)
        return
    if pids:
        print(f"  ✓ reaped Chrome left outside the session: {', '.join(map(str, pids))}")

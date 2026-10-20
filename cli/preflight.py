"""Settings-free gate for the CLI: which checkout may act on a home.

First start publishes identity through cli.start_intent before Settings loads.
Every `meiva` invocation passes `require_own_checkout` first.
"""

from __future__ import annotations

import sys
from pathlib import Path


def require_own_checkout(args_in: list[str], repo: Path) -> int | None:
    """Refuse every command from a checkout that is not the home's own.

    A home that carries its own `<home>/source` checkout is operated only by that
    checkout's CLI (`base.paths.home.home_checkout_error`); a home with
    none (a test home) accepts any checkout. There is no read-only exemption: a
    development CLI that needs a home names a temporary `MEIVA_HOME`. The one pass is
    an argv of nothing or of a lone `-h`/`--help`, which only parses and never
    reaches a verb. Returns None to proceed, an error rc to refuse.

    `repo` is the checkout the running CLI belongs to. This gate is settings-free
    and runs before every other one, so a foreign checkout is stopped before
    anything loads the home's configuration. The refusal does not echo the argv: it
    can carry a secret (`meiva config set KEY=...`).
    """
    from base.paths.home import home_checkout_error

    if args_in in ([], ["-h"], ["--help"]):
        return None
    error = home_checkout_error(repo)
    if error is None:
        return None
    print(f"✗ meiva: {error}", file=sys.stderr)
    return 1


# meiva:note
# Whether this home's meiva-root is not running: a repeated stop needs no gateway fetch.
#
# The same machine fact `root_driver.require_root_absent` proves: the root's instance
# lock is free.
def unit_already_stopped() -> bool:
    from base.native_process.root_control import instance_lock
    from base.paths import root_run_dir
    from base.paths.home import resolve_meiva_home

    try:
        fd = instance_lock.acquire_instance_lock(root_run_dir(resolve_meiva_home()))
    except instance_lock.AlreadyRunningError:
        return False
    instance_lock.release_instance_lock(fd)
    return True

# meiva:note
# The two ways the ops daemon leaves its process: past a worker thread that never returns, and
# with the exit code it was handed.
#
# Both end in the shared `hard_exit`, which skips interpreter teardown (a wedged worker thread
# would otherwise be joined forever). The scenario is the first argument.

import pathlib
import sys
import time

from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from services.agent_runner.agent_ops import boot, daemon


def _wedged_arm(home: pathlib.Path, ready: pathlib.Path) -> None:
    config = ConfigBoot()
    config.boot()
    runtime = config.ensure_eager()
    authority = ConfigAuthority(
        runtime,
        runtime if runtime.profile is None else config.complete_read_model(),
        home / ".env",
    )
    pool = boot.op_thread_pool(authority=authority)
    pool.submit(lambda: (ready.write_text("1"), time.sleep(3600)))
    while not ready.exists():
        time.sleep(0.01)
    boot.shutdown_op_pool(pool)
    daemon.hard_exit(0)


if __name__ == "__main__":
    if sys.argv[1] == "wedged_arm":
        _wedged_arm(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
    elif sys.argv[1] == "hard_exit":
        daemon.hard_exit(int(sys.argv[2]))
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")

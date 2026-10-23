"""Child process for test_read_only_cli_imports.py.

Runs the read-only `ci` CLI in a fresh interpreter whose import system rejects the
owner-only modules, so the test can prove a status read never loads them. Started
with `-I`: neither the script directory nor the working directory is on sys.path,
so the repository root arrives as `argv[1]` and the CLI arguments as JSON in `argv[2]`.
"""

from __future__ import annotations

import importlib.abc
import json
import os
import subprocess
import sys
from collections.abc import Sequence

_OWNER_ONLY = frozenset(
    {"scripts.ci.pull_requests.owner_operations", "scripts.ci.pull_requests.trunk_api"}
)


class RejectOwnerImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object, target: object = None) -> None:
        if fullname in _OWNER_ONLY:
            raise AssertionError("read-only CLI imported owner operations: " + fullname)


sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, RejectOwnerImports())
os.environ.pop("TRUNK_API_TOKEN", None)
os.environ["CI_QUEUE"] = "unrelated-owner-queue"

from base.clock import Clock, ClockConfig  # noqa: E402
from base.clock.tests.manual_timers import ManualTimers  # noqa: E402
from base.host.net.tests.fake_url_opener import FakeUrlOpener  # noqa: E402
from base.host.system.tests.fake_commands import fake_host  # noqa: E402
from scripts.ci import cli as ci_utils  # noqa: E402


def _all_passed_gh(
    argv: Sequence[str], *_args: object, **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    cmd = list(argv)
    url = cmd[2] if len(cmd) > 2 else ""
    if "api" not in cmd:
        stdout = json.dumps(
            {
                "mergeable": "MERGEABLE",
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": "backend",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "workflowName": "CI",
                        "completedAt": None,
                    }
                ],
                "headRefOid": "deadbeef",
                "isDraft": False,
            }
        )
    elif "status=completed" in url:
        stdout = "1"
    else:
        stdout = "[]"
    return subprocess.CompletedProcess(cmd, 0, stdout, "")


timers = ManualTimers()
clock = Clock(
    ClockConfig(timezone="UTC", authoritative_timezone="UTC", message_timestamp_weekday=False),
    timers=timers,
    now=timers.now,
)
raise SystemExit(
    ci_utils.main(
        json.loads(sys.argv[2]),
        host=fake_host(_all_passed_gh),
        clock=clock,
        open_url=FakeUrlOpener(),
    )
)

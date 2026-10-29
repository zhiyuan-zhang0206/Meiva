# meiva:note
# A schedule manager built the way the daemon builds it, from values a test chooses.
#
# The daemon reads settings, the home and the session environment once and hands the manager
# values. A test hands it these: the policy's limits, the home of the test's process, and a small
# launch environment. ``NOW`` is a monotonic reading; a test that needs time to pass adds to it.

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from base.deploy.lifecycle.tests.serving_root import observe
from base.paths import meiva_home
from base.sessions.protocol import SessionBackend
from services.wake.schedule_manager.manager import REPO_ROOT, ScheduleManager
from services.wake.schedule_manager.manager_policy import ManagerPolicy

POLICY = ManagerPolicy(
    auto_restart_max_per_window=5,
    auto_restart_window_seconds=3600.0,
    recovery_gate_alert_seconds=300.0,
    timezone="Asia/Shanghai",
)
NOW = 1000.0
LAUNCH_ENV = {"PATH": "/usr/bin"}


def schedule_manager(
    pool: ConnectionPool[Any],
    backend: SessionBackend,
    *,
    policy: ManagerPolicy = POLICY,
    repo_root: Path = REPO_ROOT,
    launch_env: Mapping[str, str] = LAUNCH_ENV,
) -> ScheduleManager:
    return ScheduleManager(
        pool,
        backend=backend,
        repo_root=repo_root,
        home=meiva_home(),
        policy=policy,
        launch_env=launch_env,
        observe=observe,
    )

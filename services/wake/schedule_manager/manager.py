# meiva:note
# Run due declarative schedules on the elected unit of each machine.

import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.clock import Clock
from base.native_process.process_control import ProcessControl
from services.wake.schedule_manager import revisions
from services.wake.schedule_manager.declarative import DeclarativeScheduleEngine
from services.wake.schedule_manager.run_custody import SYSTEM_PROCESS_IDENTITY_CONTROL

POLL_INTERVAL_S = 5.0
REPO_ROOT = Path(__file__).resolve().parents[3]


class ScheduleManager:
    def __init__(
        self,
        db_pool: ConnectionPool,
        *,
        repo_root: Path,
        home: Path,
        launch_env: Mapping[str, str],
        machine_name: str,
        process_control: ProcessControl,
        secret_env_resolver: Callable[[int, tuple[str, ...]], Mapping[str, str]] | None = None,
        clock: Clock,
    ) -> None:
        self._pool = db_pool
        self._home = home
        self._machine_name = machine_name
        self._lock = threading.Lock()
        self._declarative = DeclarativeScheduleEngine(
            db_pool,
            home=home,
            repo_root=repo_root,
            machine_name=machine_name,
            launch_env=launch_env,
            clock=clock,
            process_control=process_control,
            identity_control=SYSTEM_PROCESS_IDENTITY_CONTROL,
            secret_env_resolver=secret_env_resolver,
        )

    @property
    def machine_name(self) -> str:
        return self._machine_name

    def is_active_unit(self) -> bool:
        return revisions.is_active_unit(self._pool, self._machine_name, self._home)

    def reconcile(self) -> None:
        # meiva:note
        # Split units share schedule rows. Only the elected home may launch children.
        if not self.is_active_unit():
            return
        with self._lock:
            self._declarative.reconcile()

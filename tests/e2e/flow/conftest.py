# meiva:note
# Schedule and shell TTL scenarios need the same service owners as a started cluster.
from __future__ import annotations

import os
import sys
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.e2e._credentials import current_credentials
from tests.e2e._ports import reserve_health_port, wait_for_port
from tests.e2e.process_support import SERVING_READER, managed_proc


@contextmanager
def _gateway_service(gate: str, label: str, module: str) -> Generator[None]:
    reservation = reserve_health_port(label)
    port = reservation.port
    env = dict(os.environ)
    env.update(current_credentials().service_env("gateway"))
    env[f"MEIVA_{label.upper().replace('-', '_')}_HEALTH_PORT"] = str(port)
    log = Path(__file__).resolve().parents[3] / "tmp" / "e2e-logs" / f"{label}-{os.getpid()}.log"
    reservation.release()
    with managed_proc(
        [sys.executable, "-m", "tests.e2e.process_support", SERVING_READER, gate, module],
        env=env,
        label=label,
        log_path=str(log),
    ) as proc:
        wait_for_port("127.0.0.1", port, timeout=30.0, process=proc, label=label)
        yield


@pytest.fixture
def schedule_manager_proc(spawned_agent: int, gateway_proc: str) -> Iterator[None]:
    with _gateway_service(
        gateway_proc, "schedule-manager", "services.wake.schedule_manager.daemon"
    ):
        yield


@pytest.fixture
def ttl_reaper_proc(spawned_agent: int, gateway_proc: str) -> Iterator[None]:
    with _gateway_service(gateway_proc, "ttl-reaper", "services.upkeep.ttl_reaper.daemon"):
        yield

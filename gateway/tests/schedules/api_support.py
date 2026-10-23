# meiva:note
# Clients and a consumer for the schedule API's tests.
#
# A start / stop / restart / edit / delete queues a sync request and waits for the schedule-manager
# service to consume it. `schedule_client` shortens that wait, so a test with no service answers
# fast; `converging` is the service itself (the real request loop over a fake session backend),
# for a test that wants the converged outcome.

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from psycopg_pool import ConnectionPool

from base.config.tests.configuration import snapshot_process_config
from gateway.http.config import gateway_config_from_boot
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from services.wake.schedule_manager import requests
from services.wake.schedule_manager.tests.manager_support import schedule_manager
from services.wake.schedule_manager.tests.session_fakes import Backend

__all__ = ["converging", "schedule_client"]


# meiva:note
# A gateway client whose requests wait only `wait_s` for a manager to consume them.
@contextmanager
def schedule_client(*, wait_s: float = 0.1, **kwargs: Any) -> Generator[GatewayTestClient]:
    config = replace(
        gateway_config_from_boot(snapshot_process_config()),
        human_auth="required",
        schedule_consume_wait_seconds=wait_s,
        schedule_consume_poll_seconds=0.01,
    )
    with gateway_test_client(config=config, **kwargs) as client:
        yield client


# meiva:note
# The manager's request loop, consuming requests while the test runs.
@contextmanager
def converging(pool: ConnectionPool[Any], backend: Backend) -> Generator[None]:
    manager = schedule_manager(pool, backend)
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(0.02):
            requests.consume_requests(pool, manager, now=time.monotonic())

    worker = threading.Thread(target=loop, name="converging-schedule-manager")
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join()

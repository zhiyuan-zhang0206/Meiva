# meiva:note
# A resident service started the way a supervisor starts it, against the test's own cluster.
#
# `python -m <module>` runs in a child process with the test home as its `MEIVA_HOME`, whose `.env`
# names the throwaway Postgres and Redis the test fixtures provisioned, so the daemon boots through
# its real `main()`: config boot, schema gate, logging, health endpoint, work, signal handling and
# the bounded exit. A test reads the daemon's answers over its health port, stops it with SIGTERM
# and asserts what it left behind (exit code, pidfile, log).

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.request
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = ["DaemonProcess", "prepare_home", "run_to_exit", "running_daemon"]

_REPO = Path(__file__).resolve().parents[3]
_READY_TIMEOUT_S = 60.0
_STOP_TIMEOUT_S = 30.0


@dataclass
class DaemonProcess:
    process: subprocess.Popen[bytes]
    log_path: Path
    health_port: int

    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    def healthz(self) -> dict[str, Any]:
        url = f"http://127.0.0.1:{self.health_port}/healthz"
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read())

    # meiva:note
    # SIGTERM the daemon and return its exit code, killing it if it does not leave in time.
    def stop(self) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            raise AssertionError(f"the daemon ignored SIGTERM:\n{self.log()}") from None


# meiva:note
# Wait until the health endpoint answers, or fail with the daemon's log.
def _wait_ready(daemon: DaemonProcess) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if daemon.process.poll() is not None:
            raise AssertionError(
                f"the daemon exited with {daemon.process.returncode} before it served:\n"
                f"{daemon.log()}"
            )
        try:
            daemon.healthz()
        except OSError:
            time.sleep(0.1)
        else:
            return
    daemon.process.kill()
    raise AssertionError(f"the daemon never served its health endpoint:\n{daemon.log()}")


# meiva:note
# The child environment and `.env` of a daemon that boots against the provisioned cluster.
#
# `home` is the test's `MEIVA_HOME`. Its `.env` is written from the provisioned data plane, so the
# daemon reads its cluster facts the way a deployed one does instead of fetching them from a gateway.
def prepare_home(
    home: Path, machine_name: str, db_url: str, redis_url: str, extra: Mapping[str, str]
) -> dict[str, str]:
    facts = {
        "MEIVA_MACHINE_NAME": machine_name,
        "MEIVA_DB_URL": db_url,
        "MEIVA_REDIS_URL": redis_url,
        "MEIVA_CLUSTER_SECRET": os.environ["MEIVA_CLUSTER_SECRET"],
        "MEIVA_MACHINE_SERVE_GATEWAY": "true",
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "MEIVA_TELEMETRY_OTLP_ENABLED": "false",
        **extra,
    }
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in facts.items()))
    return {key: value for key, value in os.environ.items() if key != "MEIVA_CONFIG_FETCH"}


# meiva:note
# Start the daemon `command` (a literal `python -m <module>` the caller names) against the cluster
# the test fixtures provisioned and wait until it serves.
@contextmanager
def running_daemon(
    command: Sequence[str],
    *,
    home: Path,
    health_port: int,
    machine_name: str,
    db_url: str,
    redis_url: str,
    extra: Mapping[str, str] = MappingProxyType({}),
) -> Generator[DaemonProcess]:
    env = prepare_home(home, machine_name, db_url, redis_url, extra)
    log_path = home / f"{machine_name}.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=_REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        daemon = DaemonProcess(process, log_path, health_port)
        try:
            _wait_ready(daemon)
            yield daemon
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


# meiva:note
# Start the daemon `command` and return its exit code and log once it leaves on its own.
def run_to_exit(
    command: Sequence[str],
    *,
    home: Path,
    machine_name: str,
    db_url: str,
    redis_url: str,
    extra: Mapping[str, str] = MappingProxyType({}),
) -> tuple[int, str]:
    env = prepare_home(home, machine_name, db_url, redis_url, extra)
    log_path = home / f"{machine_name}-exit.log"
    with log_path.open("wb") as log:
        done = subprocess.run(  # noqa: S603
            command,
            cwd=_REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=_READY_TIMEOUT_S,
            check=False,
        )
    return done.returncode, log_path.read_text(errors="replace")

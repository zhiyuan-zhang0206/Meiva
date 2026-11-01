#!/usr/bin/env python
# meiva:note
# Version a script while no agent host serves the home, using the installed runtime.
#
# This file may be copied from a reviewed revision and run outside the installed
# checkout. All application imports come from --source, never from this file's
# checkout. The existing installed schedule writer owns the transaction, version
# snapshot and deferred convergence request; this adapter never launches a session.

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from psycopg import Connection
from psycopg_pool import ConnectionPool

# meiva:note
# The two positions this adapter reads out of the installed writer's row: the gateway's
# `_FULL_QUERY` puts the facts before `updated_at` first, then machine, owner_plugin, script.
_UPDATED_AT_COLUMN = 8
_SCRIPT_COLUMN = 11


def script_sha256(script: str) -> str:
    """Digest the UTF-8 stored text without newline conversion."""
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


# meiva:note
# Refuse while an agent host serves `home`: it holds the host's instance lock.
def require_agent_host_stopped(home: Path) -> None:
    from services.agent_runner.agent_host.lifecycle.instance import host_serving

    if host_serving(home):
        raise RuntimeError("script repair requires the agent host to be stopped")


# meiva:note
# Verify the operator interpreter, home, environment and admitted source checkout.
# A copied operator script must not import its containing development tree.
def check_runtime_admission(
    home: Path,
    source: Path,
    *,
    operator_home: Path,
    env: Mapping[str, str],
) -> None:
    if Path(sys.prefix).resolve() != (source / ".venv").resolve():
        raise RuntimeError("run this tool with the source checkout's own .venv interpreter")
    sys.path.insert(0, str(source))
    from base.host.env.boot_actions import skip_config_fetch
    from base.host.env.bootstrap import LAUNCHER_PROFILE_ENV_KEY
    from base.host.env.process_profile import MEIVA_PROCESS_PROFILE_ENV

    if operator_home.expanduser().resolve() != home:
        raise RuntimeError("--home must match this operator's MEIVA_HOME")
    if env.get(MEIVA_PROCESS_PROFILE_ENV) or env.get(LAUNCHER_PROFILE_ENV_KEY):
        raise RuntimeError("run as a local operator without a launcher credential delivery")
    skip_config_fetch()
    from base.host.db_authority.delivery import require_admitted_runtime
    from base.paths import repo_root

    if repo_root().resolve() != source:
        raise RuntimeError("loaded application modules do not belong to the requested source")
    require_admitted_runtime(home, code_root=source)


def load_installed_runtime(home: Path, source: Path) -> None:
    """Select the explicit installed source and let its owner check admission."""
    if Path(sys.prefix).resolve() != (source / ".venv").resolve():
        raise RuntimeError("run this tool with the source checkout's own .venv interpreter")
    sys.path.insert(0, str(source))
    from base.host.env.registry import process_environment
    from base.paths.home import resolve_meiva_home

    check_runtime_admission(
        home,
        source,
        operator_home=resolve_meiva_home(),
        env=process_environment(),
    )


# meiva:note
# Check the old text and reuse the installed versioned writer.
#
# The caller holds the home's lifecycle mutex and has proved no agent host serves
# the home. Per-row commits are deliberate: after an interruption, the prepared
# text is a no-op and any third text is refused.
def replace_script(
    pool: ConnectionPool[Connection], schedule_id: int, script: str, expected_sha256: str
) -> str:
    # First-generation bootstrap must use the already installed versioned writer.
    # Calling a newly introduced public owner would require deploying it first.
    # This narrow coupling is covered on 44d and current source; no SQL is copied.
    from gateway.schedules.router import fetch_full_blocking, update_blocking

    previous = fetch_full_blocking(pool, schedule_id)
    actual = script_sha256(previous[_SCRIPT_COLUMN])
    prepared = script_sha256(script)
    if actual == prepared:
        return "unchanged"
    if actual != expected_sha256:
        raise RuntimeError(
            f"schedule {schedule_id}: expected old script {expected_sha256}, found {actual}"
        )
    row, _needs_sync = update_blocking(pool, schedule_id, {"script": script})
    # meiva:note
    # `fetch_full_blocking` returns `_FULL_QUERY`: id, name, description, command, enabled,
    # status, last_error, created_at, updated_at, machine, owner_plugin, script, then the
    # placement columns the join adds. Only the script and updated_at may move, so the
    # comparison takes the facts before updated_at and the script at its own index.
    if row[:_UPDATED_AT_COLUMN] != previous[:_UPDATED_AT_COLUMN] or (row[_SCRIPT_COLUMN] != script):
        raise RuntimeError(f"schedule {schedule_id}: installed writer changed protected fields")
    return "updated"


# meiva:note
# The installed source supplied by the operator owns this image. A copied
# adapter's __file__ must not lend its checkout identity to the installed writer.
def source_version(source: Path) -> Callable[[], int]:
    from base.native_process.code_version import CodeVersion
    from base.native_process.loaded_commit import LoadedCommit

    image = LoadedCommit.capture(source_root=source)
    return CodeVersion(image).get


@contextmanager
def repair_guard(home: Path, *, pid: int, lock_timeout_s: float = 30.0) -> Generator[None]:
    from base.deploy.lifecycle.home_lifecycle_locks import resource_lock

    with resource_lock(
        home=home, pid=pid, purpose="operator.update_schedule_script", timeout_s=lock_timeout_s
    ):
        require_agent_host_stopped(home)
        yield


# meiva:note
# Run one script-only edit while no agent host serves `home`.
def repair_script(
    *,
    home: Path,
    source: Path,
    schedule_id: int,
    script: str,
    expected_sha256: str,
    pid: int,
) -> str:
    load_installed_runtime(home, source)
    from base.db import Database
    from base.db.code_version_gate import ProcessDbGate
    from base.telemetry import process_name

    with repair_guard(home, pid=pid):
        gate = ProcessDbGate(version=source_version(source), process=process_name())
        with Database.from_settings(gate=gate).pool() as pool:
            return replace_script(pool, schedule_id, script, expected_sha256)


def old_script_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256 hex digest")
    return value


def positive_id(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("schedule id must be positive")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schedule_id", type=positive_id)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--script-file", required=True)
    parser.add_argument("--expected-sha256", required=True, type=old_script_digest)
    args = parser.parse_args(argv)
    raw = (
        sys.stdin.buffer.read() if args.script_file == "-" else Path(args.script_file).read_bytes()
    )
    script = raw.decode("utf-8")
    compile(script, "<schedule>", "exec")
    result = repair_script(
        home=args.home.expanduser().resolve(),
        source=args.source.expanduser().resolve(),
        schedule_id=args.schedule_id,
        script=script,
        expected_sha256=args.expected_sha256,
        pid=os.getpid(),
    )
    print(f"schedule {args.schedule_id}: {result} script_sha256={script_sha256(script)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# meiva:note
# Settings-free input boundary of ``meiva start``.
#
# Start admits a home `meiva init` initialized; it takes no identity input of its own
# (`cli/init_intent.py` does). Runtime Settings are imported only after the home is
# admitted. The home and checkout it acts on arrive as a `HomeSite` the entry read once
# (`cli.main`), behind the checkout gate (`cli.preflight`).
#
# `admitted_home` is the settings-free half: it admits the home, holds its lifecycle lock
# and enters this process into it. `start_once` admits the runtime inside it, opens the
# loguru sinks and runs the lifecycle, and `cli.startup.outcome` decides what the finished
# start owes the boot unit and the boot scheduler.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psutil
from pydantic import ValidationError

from base import telemetry
from base.deploy import start_trace
from base.host.db_authority.delivery import GENERATION_ENV
from base.host.env.registry import derived_env_keys, env_identity_keys
from base.host.permissions_helper.client import HelperFactory
from base.host.system import boot_policy
from base.host.system.hostport import Host
from base.native_process.os_platform import file_lock, is_macos
from base.paths.private_dir import ensure_private_dir
from cli.database import OperatorDatabaseFactory, OperatorEventPipeline
from cli.start_identity import require_initialized
from cli.start_runtime import HomeSite, StartRuntime
from cli.startup.log_sinks import START_SINK
from cli.startup.outcome import (
    BootDone,
    BootGaveUp,
    BootRetry,
    plan_after_start,
    plan_boot_settlement,
)
from cli.startup.settings_failure import settings_failure_text


# meiva:note
# Make this process the admitted home's: its `.env` is the only source of its identity.
#
# A delivered login and its generation are discarded together so Settings re-admits database
# authority for this home.
def _enter_home(home: Path) -> None:
    for key in derived_env_keys() | env_identity_keys() | {GENERATION_ENV}:
        os.environ.pop(key, None)
    os.environ["MEIVA_HOME"] = str(home)


# meiva:note
# Admit the home, hold its lifecycle lock and enter this process into it.
#
# Raises why the home cannot start before anything is created: a start never makes a
# home, so the lock below can rely on the directory existing. Once entered, the process's
# configuration is built from the home's own `.env`.
@contextmanager
def admitted_home(home: Path) -> Generator[None]:
    require_initialized(home)
    ensure_private_dir(home)
    with file_lock(home / "start-intent.lock", timeout_s=30):
        require_initialized(home)
        _enter_home(home)
        yield


# meiva:note
# Do what a start that exited with `result` owes the boot unit (`plan_after_start`).
#
# The imports are lazy: they run after the lifecycle, when runtime Settings exist.
def finish_start(
    result: int,
    *,
    helper: HelperFactory | None,
    host: Host,
    home: Path,
    proc_root: Path = Path("/proc"),
) -> None:
    step = plan_after_start(result)
    if step is None:
        return
    from cli.commands.lifecycle.root_driver import complete_boot_start

    try:
        complete_boot_start(helper=helper, host=host, home=home, proc_root=proc_root)
    except (RuntimeError, OSError, TimeoutError):
        from base.deploy.lifecycle.start_serving import clear_serving

        clear_serving(home)
        raise


def start_once(
    args: argparse.Namespace,
    site: HomeSite,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    database_factory: OperatorDatabaseFactory,
    producer: OperatorEventPipeline,
    boot: bool,
) -> int:
    try:
        with admitted_home(site.home):
            # meiva:note
            # The runtime is admitted under the home's lock, so no other lifecycle operation
            # changes the source while it is hashed. It needs no Settings and fails before any
            # sink opens.
            runtime = StartRuntime.admit(site.checkout)
            # meiva:note
            # Building the sinks builds Settings, so they open only once the home is admitted.
            producer.initialize_logging(START_SINK)
            # meiva:note
            # Runtime delivery is imported here, after admission, so that admission
            # stays settings-free.
            from cli.commands.lifecycle.start import cmd_start

            result = cmd_start(
                disabled_services=tuple(args.disable_service or ()),
                only_services=tuple(args.only_service or ()),
                all_services=args.all_services,
                runtime=runtime,
                retained_children=retained_children,
                database_factory=database_factory,
                producer=producer,
                command=args.runtime,
                boot_attempt=boot,
            )
            finish_start(result, helper=args.runtime.helper, host=args.runtime.host, home=site.home)
            return result
    except ValidationError as exc:
        print(settings_failure_text(exc), file=sys.stderr)
        return 1
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        print(f"meiva start: {exc}", file=sys.stderr)
        return 1
    finally:
        report = start_trace.report(site.home)
        if report is not None:
            print()
            print(report)


# meiva:note
# Tell the boot scheduler how boot-job attempt number `attempt`, which exited with `code`, went.
#
# The count never refuses a start (see `base.host.system.boot_policy`); it only decides
# what a *failed* attempt tells its scheduler. Returns the job's exit code.
def settle_boot_attempt(home: Path, attempt: int, code: int, *, macos: bool) -> int:
    plan = plan_boot_settlement(
        code,
        attempt=attempt,
        max_attempts=boot_policy.BOOT_MAX_ATTEMPTS,
        spent=boot_policy.budget_spent(attempt),
    )
    match plan:
        case BootDone():
            boot_policy.clear_attempts(home)
            return 0
        case BootRetry(exit_code):
            return exit_code
        case BootGaveUp(attempt=attempts, failed_code=failed, message=message):
            print(message, file=sys.stderr)
            telemetry.emit(
                "telemetry",
                "boot_retries_exhausted",
                level="error",
                source="start",
                attributes={"attempts": attempts, "exit_code": failed},
            )
            return boot_policy.exit_code_after_budget(failed, macos=macos)


# meiva:note
# `meiva start`: one start, or one boot-job attempt that settles with its scheduler.
def run_start(
    args: argparse.Namespace,
    site: HomeSite,
    *,
    retained_children: list[subprocess.Popen[bytes]],
    database_factory: OperatorDatabaseFactory,
    producer: OperatorEventPipeline,
) -> int:
    if not args.boot:
        return start_once(
            args,
            site,
            retained_children,
            database_factory=database_factory,
            producer=producer,
            boot=False,
        )
    attempt = boot_policy.begin_attempt(site.home, boot_time=psutil.boot_time())
    code = start_once(
        args,
        site,
        retained_children,
        database_factory=database_factory,
        producer=producer,
        boot=True,
    )
    return settle_boot_attempt(site.home, attempt, code, macos=is_macos())

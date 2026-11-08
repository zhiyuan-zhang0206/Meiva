# meiva:note
# `meiva jobs heartbeats`: the heartbeat expectation set of this machine, as JSON.
#
# The declaration lives in the repository (`base.host.system.cron.heartbeat_population`), but the machine
# that reads it — a liveness census, a dashboard, a person — is not obliged to import this
# checkout. Printing one document keeps a single reader format while letting a consumer outside
# the repository compute both directions of the diff: a job declared here but missing from the
# scheduler, and a job in the scheduler that no declaration claims.
#
# It fails instead of printing an empty set. "This machine expects nothing" and "the declarations
# could not be read" look identical to a consumer, and the second one is the one that would let a
# stopped job pass as a machine with nothing to run.

from __future__ import annotations

import json
import sys

from base.cluster.derive import production_identity
from base.host.system.cron.heartbeat_population import (
    MachinePolicy,
    declarations_document,
)
from base.host.system.job_values import JobRoots
from base.native_process.loaded_commit import capture_commit
from base.paths.home import is_default_home_of
from cli.database.runtime import CommandRuntime

__all__ = ["cmd_jobs_heartbeats"]


def _policy(*, runtime: CommandRuntime) -> MachinePolicy:
    view = runtime.config.view
    refresh = view.packages.refresh_enabled
    return MachinePolicy(
        jobs_enabled=view.general.os_jobs_enabled,
        owned_home=is_default_home_of(runtime.home, user_home=runtime.session.user_home),
        packages_refresh=refresh,
        packages_tick_seconds=view.packages.refresh_tick_seconds if refresh else None,
        walg_configured=view.walg.walg_config_file is not None,
        production=production_identity(machine=runtime.machine, home=runtime.home),
        backup_hour=view.services.backup_hour,
    )


# meiva:note
# Print the declared heartbeat set of this machine; non-zero when it cannot be read, so a
# consumer never receives an empty set as an answer.
def cmd_jobs_heartbeats(*, runtime: CommandRuntime) -> int:
    roots = JobRoots(home=runtime.home, repo=runtime.checkout, user_home=runtime.session.user_home)
    today = runtime.clock.now().astimezone(runtime.clock.explicit_zone()).date()
    try:
        document = declarations_document(
            machine=runtime.machine.name(),
            repo_sha=capture_commit(runtime.checkout),
            roots=roots,
            uid=runtime.host.uid,
            policy=_policy(runtime=runtime),
            today=today,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"meiva jobs heartbeats: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=1, sort_keys=True))
    return 0

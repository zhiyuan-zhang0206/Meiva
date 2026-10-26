# meiva:note
# The extras a stop retires: the macOS permissions helper outside the service roster.
#
# Normal stop and destroy share exact-home helper retirement. Start recreates its definition,
# first rebuilding the signed artifact when its sources changed (only a retired helper's artifact
# is replaced). Updates and restart preserve the helper.

from __future__ import annotations

from cli.commands.data_plane.stop_waits import Deadline
from cli.commands.lifecycle.stop.extras_plan import retained_refusal, retires_helper
from cli.database.runtime import CommandRuntime


# meiva:note
# Stop this home's macOS helper; a failure propagates, a non-macOS host has none.
def stop_permissions_helper(command: CommandRuntime, *, timeout_s: float) -> None:
    if not retires_helper(command.host.platform.system):
        return
    from base.host.permissions_helper.client import PermissionsHelperClient
    from services.desktop.permissions_helper.launchd_job import unregister_helper

    unregister_helper(
        command.home,
        helper_port=command.config.view.services.permissions_helper_port,
        connect=PermissionsHelperClient,
        timeout_s=timeout_s,
    )


# meiva:note
# Retire the extras within what is left of the stop's `deadline`.
#
# Refuses while any unit is retained: the helper cannot be retired under a live root.
def stop_extras(command: CommandRuntime, deadline: Deadline, *, retained: frozenset[str]) -> None:
    if (refusal := retained_refusal(retained)) is not None:
        raise ValueError(refusal)
    stop_permissions_helper(command, timeout_s=deadline.remaining())

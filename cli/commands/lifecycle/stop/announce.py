# meiva:note
# Tell the gateway, and the host's own boot job, that this unit is stopping on purpose.

from __future__ import annotations

from datetime import datetime

from base.cluster.machine import gateway_api_base, gateway_auth_headers
from base.cluster.machine.boot import gateway_access_from_boot
from base.host.net.http_dial import post as dial_post
from cli.commands.lifecycle import stand_down
from cli.commands.lifecycle.stop.choices import StopWindow
from cli.database.runtime import CommandRuntime


# meiva:note
# Record the stand-down, then best-effort POST `/api/cluster/stopping` before local teardown.
#
# Stamps the stopping unit's `stopped_at` and recomputes the composed `machines` row so the
# cluster view distinguishes an intentional `meiva stop` from a crash. `home` identifies THIS
# unit (a co-located peer keeps its caps). Best-effort by design: if the gateway is unreachable
# we are stopping anyway, so we log and proceed rather than block the teardown.
def announce_stopping(command: CommandRuntime, window: StopWindow, *, now: datetime) -> None:
    # meiva:note
    # The local half of the intent is written first: a boot job that starts this home later
    # reads it and stays down (task #5493). Writing it before the gateway call is what makes an
    # unreachable gateway cost the record nothing.
    name = command.machine.name()
    held = stand_down.record(
        command.home,
        now=now,
        until=stand_down.window_until(window.for_seconds, now=now),
        reason=window.reason,
        machine=name,
    )
    print(f"[meiva stop] stand-down recorded ({held.describe(now)})")
    try:
        access = gateway_access_from_boot(command.config)
        home = str(command.home)
        url = f"{gateway_api_base(access.gateway_url)}/api/cluster/stopping"
        params = {"machine": name, "home": home}
        if window.for_seconds is not None:
            params["maintenance_for_seconds"] = str(window.for_seconds)
        if window.reason is not None:
            params["maintenance_reason"] = window.reason
        resp = dial_post(
            url,
            params=params,
            timeout=5.0,
            headers=gateway_auth_headers(access),
        )
        resp.raise_for_status()
        print(f"[meiva stop] announced intentional shutdown of {name!r} ({home}) to the gateway")
    except Exception as e:
        print(f"[meiva stop] could not announce shutdown (proceeding anyway): {e}")

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from base.host.permissions_helper.grants import checklist
from base.host.permissions_helper.grants.gate import GateReport, GateState
from services.desktop.permissions_helper import onboarding
from services.desktop.permissions_helper.onboarding import GateHost


def run(
    args: argparse.Namespace,
    host: GateHost | None = None,
    *,
    now: datetime | None = None,
) -> int:
    verb = args.permissions_cmd
    if verb == "status":
        return _report(onboarding.check(host=host, now=now), as_json=args.json)
    if verb == "onboard":
        return _report(
            onboarding.onboard(
                host=host,
                now=now,
                automatic=False,
                timeout_s=args.timeout,
                progress=sys.stderr,
            ),
            as_json=args.json,
        )
    if verb == "defer":
        onboarding.defer(now=now, host=host)
        print(
            "Recorded. Nothing is granted by this: the host stays in its limited state and"
            " the next start asks again."
        )
        return 0
    if verb == "restart-helper":
        print(onboarding.restart_helper())
        return 0
    raise SystemExit(f"unknown permissions verb {verb!r}")


def _report(report: GateReport, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(checklist.payload(report), indent=2))
    else:
        print(checklist.render_text(report))
    return 0 if report.complete or report.state is GateState.NOT_APPLICABLE else 1

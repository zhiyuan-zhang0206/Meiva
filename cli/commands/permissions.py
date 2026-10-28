from __future__ import annotations

import argparse
import json
import sys

from base.clock import Clock
from base.host.permissions_helper.grants import checklist
from base.host.permissions_helper.grants.gate import GateReport, GateState
from services.desktop.permissions_helper import onboarding
from services.desktop.permissions_helper.onboarding import GateHost


# meiva:note
# The `meiva permissions` verbs for this invocation. The host the gate runs on is described from the
# invocation's own facts (the home, the account home, the environment the configuration reads now,
# and the helper the platform decided); `restart-helper` prints a sentence and needs none of them.
def run(args: argparse.Namespace) -> int:
    if args.permissions_cmd == "restart-helper":
        print(onboarding.restart_helper())
        return 0
    runtime = args.runtime
    host = onboarding.gate_host(
        home=runtime.home,
        user_home=runtime.session.user_home,
        environ=runtime.host_after_config().environ,
        helper=runtime.helper,
        timers=runtime.timers,
    )
    return dispatch(args, host=host, clock=runtime.clock, run_id=runtime.new_id())


def dispatch(args: argparse.Namespace, *, host: GateHost, clock: Clock, run_id: str) -> int:
    verb = args.permissions_cmd
    if verb == "status":
        return _report(
            onboarding.check(host=host, now=clock.now(), run_id=run_id), as_json=args.json
        )
    if verb == "onboard":
        return _report(
            onboarding.onboard(
                host=host,
                clock=clock,
                run_id=run_id,
                automatic=False,
                timeout_s=args.timeout,
                progress=sys.stderr,
            ),
            as_json=args.json,
        )
    if verb == "defer":
        onboarding.defer(host=host, now=clock.now())
        print(
            "Recorded. Nothing is granted by this: the host stays in its limited state and"
            " the next start asks again."
        )
        return 0
    raise SystemExit(f"unknown permissions verb {verb!r}")


def _report(report: GateReport, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(checklist.payload(report), indent=2))
    else:
        print(checklist.render_text(report))
    return 0 if report.complete or report.state is GateState.NOT_APPLICABLE else 1

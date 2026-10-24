# meiva:note
# Report actual imports in a fresh child, before any test harness imports the
# application roster. Public builders must retain their original object identity.
from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("surface", choices=["model", "public"])
    args = parser.parse_args()
    if args.surface == "model":
        from services.supervision.roster import ServiceSpec as PublicServiceSpec
        from services.supervision.roster.service_spec import ServiceSpec

        same_identity = PublicServiceSpec is ServiceSpec
    else:
        from services.supervision.roster import daemon_identity, healthz_daemon
        from services.supervision.roster.healthz import daemon_identity as owned_identity
        from services.supervision.roster.healthz import healthz_daemon as owned_healthz

        same_identity = daemon_identity is owned_identity and healthz_daemon is owned_healthz
    observers = (
        "services.supervision.roster.composition",
        "services.supervision.roster.plugin_services",
        "services.entrypoints.gate.daemon",
        "services.supervision.healthchecks.protocol_probe",
        "services.supervision.healthchecks.lgtm",
        "services.desktop.browser.probe",
    )
    sys.stdout.write(
        json.dumps(
            {
                "same_identity": same_identity,
                "loaded_observers": [name for name in observers if name in sys.modules],
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

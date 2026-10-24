from __future__ import annotations

from pathlib import Path

from base.cluster.machine import MachineIdentity
from base.telemetry.lgtm_local import BACKENDS, backend_urls
from cli.commands.observability._lgtm_provisioning import verify_dashboard_provisioning
from services.supervision.healthchecks.lgtm import is_lgtm_host, probe_backend


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def cmd_lgtm_status(*, home: Path, machine: MachineIdentity) -> int:
    station = is_lgtm_host(machine=machine)
    if not station:
        print("this home is not an observability station")
    print_lgtm_status()
    if not station:
        return 0
    problems = verify_dashboard_provisioning(
        home / "lgtm" / "native", grafana_url=backend_urls()["grafana"]
    )
    for problem in problems:
        print(f"  dashboard: {problem}")
    if not problems:
        print("  dashboard: meiva-ops-main served from folder Meiva")
    return 1 if problems else 0


# meiva:note
# Print each backend's probe verdict and detail.
def print_lgtm_status() -> None:
    for name in BACKENDS:
        result = probe_backend(name)
        print(f"  {name}: {result.verdict.value} ({result.detail})")

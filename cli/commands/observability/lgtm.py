from __future__ import annotations

from pathlib import Path

from base.cluster.machine import MachineIdentityReader
from base.telemetry.lgtm_local import BACKENDS, LgtmSettings, backend_urls
from cli.commands.observability._lgtm_provisioning import (
    verify_alert_rule_provisioning,
    verify_dashboard_provisioning,
)
from services.supervision.healthchecks.lgtm import (
    is_lgtm_host,
    lgtm_roles,
    probe_backend,
    probe_tempo,
)


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.md
def cmd_lgtm_status(
    *, home: Path, machine: MachineIdentityReader, observability: LgtmSettings
) -> int:
    station = is_lgtm_host(home, roles=lgtm_roles(machine))
    if not station:
        print("this home is not an observability station")
    print_lgtm_status(observability, home=home, station=station)
    if not station:
        return 0
    native_dir = home / "lgtm" / "native"
    grafana_url = backend_urls(observability)["grafana"]
    problems = verify_dashboard_provisioning(native_dir, grafana_url=grafana_url)
    for problem in problems:
        print(f"  dashboard: {problem}")
    if not problems:
        print("  dashboard: meiva-ops-main served from folder Meiva")
    rule_problems = verify_alert_rule_provisioning(native_dir, grafana_url=grafana_url)
    for problem in rule_problems:
        print(f"  alert rules: {problem}")
    if not rule_problems:
        print("  alert rules: loaded UIDs match rendered provisioning")
    return 1 if problems or rule_problems else 0


# meiva:note
# Print each backend's probe verdict and detail.
def print_lgtm_status(observability: LgtmSettings, *, home: Path, station: bool = True) -> None:
    urls = backend_urls(observability)
    for name in BACKENDS:
        if name == "tempo" and not station:
            continue
        result = (
            probe_tempo(home=home, observability=observability)
            if name == "tempo"
            else probe_backend(name, urls=urls)
        )
        print(f"  {name}: {result.verdict.value} ({result.detail})")

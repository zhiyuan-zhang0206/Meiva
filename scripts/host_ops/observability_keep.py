#!/usr/bin/env python
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

KEEPABLE_UNITS = ("loki", "prometheus", "tempo", "grafana", "otel-collector")

RESTART_PREFIXES = (
    "deploy/lgtm/",
    "deploy/otel-collector/",
    "cli/commands/observability/",
    "cli/commands/lifecycle/lgtm_toggle.py",
    "cli/commands/lifecycle/converge_host.py",
    "cli/commands/converge/",
    "base/telemetry/",
    "base/config/domains/observability/",
    "base/config/domains/general.py",
    "base/config/domains/gateway.py",
    "base/config/domains/storage/data_plane.py",
    "base/deploy/release/collector/",
    "base/host/env/dotenv_boot.py",
    "base/host/env/port_lease.py",
    "base/host/system/macos_firewall.py",
    "base/cluster/listener_plan.py",
    "base/cluster/port_preflight.py",
    "base/paths/",
    "base/native_process/",
    "base/packages/dashboard_supply.py",
    "base/packages/plugins/",
    "base/api_contracts/metric_spec.py",
    "base/events/contract.py",
    "base/events/vocabulary.py",
    "base/events/declarations/delivery.py",
    "base/events/declarations/gateway.py",
    "base/events/declarations/maintenance.py",
    "base/events/declarations/observability.py",
    "base/events/declarations/root.py",
    "services/supervision/",
    "gateway/alerts/",
    "gateway/lgtm/",
    "gateway/routers/proxies/grafana.py",
)

RESTART_PATTERNS = (re.compile(r"^meiva_builtins/plugins/[^/]+/metrics[^/]*$"),)


@dataclass(frozen=True)
class Advice:
    hits: tuple[str, ...]

    @property
    def keep(self) -> bool:
        return not self.hits

    def render(self) -> str:
        if self.keep:
            flags = " ".join(f"--keep-service {unit}" for unit in KEEPABLE_UNITS)
            return f"keep: {flags}"
        lines = [f"restart: {len(self.hits)} changed path(s) can reach the observability stack"]
        lines += [f"  {path}" for path in self.hits]
        return "\n".join(lines)


def reaches_stack(path: str) -> bool:
    return path.startswith(RESTART_PREFIXES) or any(p.match(path) for p in RESTART_PATTERNS)


def advise(changed: list[str]) -> Advice:
    return Advice(tuple(sorted({path for path in changed if reaches_stack(path)})))


def changed_paths(old: str, new: str, *, repo: str) -> list[str]:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", repo, "diff", "--name-only", "--no-renames", "-z", old, new],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise ValueError(done.stderr.strip() or f"git diff exited {done.returncode}")
    return [path for path in done.stdout.split("\0") if path]


def main(argv: list[str]) -> int:
    args = list(argv)
    repo = "."
    if "--repo" in args:
        at = args.index("--repo")
        if at + 1 >= len(args):
            print("usage: observability_keep.py OLD NEW [--repo PATH]", file=sys.stderr)
            return 2
        repo = args[at + 1]
        del args[at : at + 2]
    if len(args) != 2:
        print("usage: observability_keep.py OLD NEW [--repo PATH]", file=sys.stderr)
        return 2
    try:
        advice = advise(changed_paths(args[0], args[1], repo=repo))
    except (ValueError, OSError) as exc:
        print(f"restart: no verdict ({exc}); treat the stack as changed")
        return 2
    print(advice.render())
    return 0 if advice.keep else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

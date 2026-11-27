# meiva:link cli/fleet_update/docs/structure.md

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from base.host.env.harness.catalog import CATALOG, needs_cli_update, parse_cli_version
from base.host.env.harness_binaries import locate_harness_binary, with_harness_path
from services.supervision.roster.service_spec import ServiceSpec


def service_readiness(roster: tuple[ServiceSpec, ...]) -> dict[str, str]:
    checks: dict[str, str] = {}
    for spec in roster:
        if spec.identity_probe is None:
            continue
        try:
            result = spec.identity_probe()
        except Exception as exc:
            checks[spec.session] = f"probe failed ({type(exc).__name__})"
        else:
            checks[spec.session] = "ready" if result.alive else result.verdict.value
    return checks


def harness_readiness(env: Mapping[str, str], *, user_home: Path) -> dict[str, str]:
    checks: dict[str, str] = {}
    for spec in CATALOG.values():
        binary, _ = locate_harness_binary(spec.binary, home=user_home, path_env=env.get("PATH", ""))
        name = f"harness/{spec.cli}"
        if binary is None:
            checks[name] = "not-installed"
            continue
        try:
            result = subprocess.run(
                [binary, "--version"],
                env=with_harness_path(env, home=user_home),
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            checks[name] = f"version probe failed ({type(exc).__name__})"
            continue
        version = parse_cli_version(result.stdout + "\n" + result.stderr)
        checks[name] = (
            f"version {version or 'unknown'} is incompatible with target {spec.pinned_version}"
            if needs_cli_update(spec, version)
            else "ready"
        )
    return checks

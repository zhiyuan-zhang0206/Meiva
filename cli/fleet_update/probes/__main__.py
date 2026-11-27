# meiva:link cli/fleet_update/docs/structure.md

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from base.config import get_field
from base.host.atomic_io import write_bytes_atomic
from base.host.env.boot_policy import MirrorRejectedError
from base.paths.home import resolve_meiva_home
from base.sessions.env_forwarding import managed_service_env
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.start import launch_roster, read_launch_inputs
from cli.commands.lifecycle.start.start_config import read_start_config
from cli.database.runtime import CommandRuntime
from services.supervision.roster.service_spec import ServiceSpec

from .config import config_generation
from .readiness import harness_readiness, service_readiness
from .receipt import DomainReceipt, decode_receipt
from .schema import schema_sets, schema_status, schema_version


class ProbeArgs(argparse.Namespace):
    phase: str
    target: str
    start_budget: int
    require_preflight: bool
    all_services: bool
    only_service: list[str]
    disable_service: list[str]


def _target(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, check=True, capture_output=True
    ).stdout.strip()


def _selection(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group(required=True)
    _ = selection.add_argument("--all-services", action="store_true")
    _ = selection.add_argument("--only-service", action="append", default=[])
    _ = selection.add_argument("--disable-service", action="append", default=[])


def _roster(args: ProbeArgs, command: CommandRuntime) -> tuple[ServiceSpec, ...]:
    return launch_roster(
        command.machine.role(),
        only_services=tuple(args.only_service),
        disabled_services=tuple(args.disable_service),
        all_services=args.all_services,
        machine=command.machine,
        host=command.host,
        home=command.home,
    )


def _domain(args: ProbeArgs, command: CommandRuntime, target: str) -> int:
    roles = command.machine.role()
    env = command.config.process_environment()
    stage = args.phase == "prepare"
    pure_runner = "agent-runner" in roles and "gateway" not in roles
    print(f"domain config: target {target}", flush=True)
    generation = config_generation(
        command.config,
        env,
        home=command.home,
        runner=pure_runner,
        stage=stage,
        now=time.time,
        minimum_lifetime=args.start_budget,
        verify_live=not stage,
    )
    print(f"domain schema: target {target}, config {generation}", flush=True)
    required, applied = schema_sets(
        command.config.view.data_plane.db_url, command.checkout / "migrations"
    )
    checks = {"config": "ready", "schema": schema_status(required, applied, allow_pending=stage)}
    if "agent-runner" in roles:
        checks.update(harness_readiness(env, user_home=command.session.user_home))
    roster = _roster(args, command)
    if stage:
        checks.update({spec.session: "selected" for spec in roster if spec.identity_probe})
    else:
        checks.update(service_readiness(roster))
    receipt = DomainReceipt(
        target,
        "runner" if pure_runner else "local",
        generation,
        schema_version(required),
        schema_version(applied),
        checks,
    )
    expected_path = command.home / "updates" / "fleet-domain-receipt.json"
    if stage:
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(expected_path, receipt.encode().encode(), mode=0o600)
    else:
        _compare_preflight(receipt, expected_path, require_preflight=args.require_preflight)
    print(receipt.line())
    return 0


def _compare_preflight(
    receipt: DomainReceipt, expected_path: Path, *, require_preflight: bool
) -> None:
    try:
        prior = decode_receipt(expected_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        receipt.components["preflight"] = "missing" if require_preflight else "legacy-unbound"
    else:
        if prior.target != receipt.target:
            receipt.components["preflight"] = f"target SHA changed (staged {prior.target})"
        if (prior.scope, prior.config) != (receipt.scope, receipt.config):
            receipt.components["config"] = f"config generation changed (staged {prior.config})"
        if prior.schema != receipt.schema:
            receipt.components["schema-contract"] = (
                f"target schema version changed (staged {prior.schema})"
            )
        for component, state in prior.components.items():
            if component not in receipt.components:
                receipt.components[component] = "missing readiness check from staged selection"
            elif (
                component.startswith("harness/")
                and state == "ready"
                and receipt.components.get(component) == "not-installed"
            ):
                receipt.components[component] = "was installed during preflight; now absent"


def _process_proof(args: ProbeArgs, command: CommandRuntime, target: str) -> int:
    roles = command.machine.role()
    env = command.config.process_environment()
    client = root_driver.root_client(command.home, environ=env, pid=command.pid)
    snapshot = root_driver.root_status(client)
    if snapshot is None:
        print(f"meiva-root: no readable live process status for target {target}")
        return 1
    generation = snapshot["root"]["launch_digest"]
    print(f"meiva-root: observed launch generation {generation} for target {target}", flush=True)
    _ = config_generation(
        command.config,
        env,
        home=command.home,
        runner="agent-runner" in roles and "gateway" not in roles,
        stage=False,
        now=time.time,
        minimum_lifetime=0,
    )
    config = read_start_config(command.config)
    inputs = read_launch_inputs(command, config)
    roster = _roster(args, command)
    env = managed_service_env(inputs.service_path, environ=inputs.environment)
    manifest = root_driver.launch_manifest(
        roster, command.checkout, roles=roles, env=env, inputs=inputs
    )
    try:
        root_driver.require_same_generation(manifest, snapshot, reconcile=True)
    except root_driver.RootDriverError as exc:
        print(f"meiva-root: target {target}, launch generation {generation}: {exc}")
        return 1
    units = {unit["id"]: unit["state"] for unit in snapshot["units"]}
    unready = [
        f"{spec.session}={units.get(spec.session, 'absent')}"
        for spec in roster
        if units.get(spec.session) != "running"
    ]
    if unready:
        print(f"meiva-root: target {target}, launch generation {generation}: " + ", ".join(unready))
        return 1
    print(f"FLEET_SUPERVISION={target} {generation} {len(roster)} running units")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("phase", choices=("prepare", "ready", "supervision"))
    _ = parser.add_argument("target")
    _ = parser.add_argument("--start-budget", type=int, default=0)
    _ = parser.add_argument("--require-preflight", action="store_true")
    _selection(parser)
    args = parser.parse_args(argv, namespace=ProbeArgs())
    try:
        command = CommandRuntime.create(resolve_meiva_home().resolve())
        _ = get_field("machine_host")
        actual = _target(command.checkout)
        if actual != args.target:
            print(f"fleet {args.phase}: checkout SHA {actual} differs from target {args.target}")
            return 1
        if args.phase == "supervision":
            return _process_proof(args, command, actual)
        return _domain(args, command, actual)
    except MirrorRejectedError as exc:
        print(f"domain config: target {args.target}: {exc.state} mirror ({exc})")
    except root_driver.RootDriverError as exc:
        print(f"meiva-root: target {args.target}: {exc}")
    except Exception as exc:
        print(f"fleet {args.phase}: target {args.target}: {type(exc).__name__}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

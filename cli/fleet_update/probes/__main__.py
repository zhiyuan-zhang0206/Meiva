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
from .schema import checkpoint_status, schema_sets, schema_status, schema_version
from .supervision import process_proof, stage_expected_launch


class ProbeArgs(argparse.Namespace):
    phase: str
    target: str
    start_budget: int
    launch_repo: Path | None
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


def _selection_key(args: ProbeArgs) -> tuple[str, ...]:
    if args.all_services:
        return ("--all-services",)
    if args.only_service:
        return ("--only-service", *sorted(set(args.only_service)))
    return ("--disable-service", *sorted(set(args.disable_service)))


def _stage_launch(
    args: ProbeArgs, command: CommandRuntime, target: str, roster: tuple[ServiceSpec, ...]
) -> None:
    repo = command.checkout if args.launch_repo is None else args.launch_repo.resolve(strict=True)
    inputs = read_launch_inputs(command, read_start_config(command.config))
    env = managed_service_env(inputs.service_path, environ=inputs.environment, venv=repo / ".venv")
    manifest = root_driver.launch_manifest(
        roster, repo, roles=command.machine.role(), env=env, inputs=inputs
    )
    stage_expected_launch(
        command.home, target=target, selection=_selection_key(args), manifest=manifest
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
    db_url = command.config.view.data_plane.db_url
    required, applied = schema_sets(db_url, command.checkout / "migrations")
    schema = schema_status(required, applied, allow_pending=stage)
    pending = required - applied if schema == "migration-pending" else set[str]()
    checks = {
        "config": "ready",
        "schema": schema,
        "checkpoint": checkpoint_status(db_url, required, pending=pending),
    }
    if "agent-runner" in roles:
        checks.update(harness_readiness(env, user_home=command.session.user_home))
    roster = _roster(args, command)
    if stage:
        checks.update({spec.session: "selected" for spec in roster if spec.identity_probe})
    else:
        checks.update(service_readiness(roster, target=target))
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
        _stage_launch(args, command, target, roster)
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("phase", choices=("prepare", "ready", "supervision"))
    _ = parser.add_argument("target")
    _ = parser.add_argument("--start-budget", type=int, default=0)
    _ = parser.add_argument("--launch-repo", type=Path)
    _ = parser.add_argument("--require-preflight", action="store_true")
    _selection(parser)
    args = parser.parse_args(argv, namespace=ProbeArgs())
    try:
        command = CommandRuntime.create(resolve_meiva_home().resolve())
        actual = _target(command.checkout)
        if actual != args.target:
            print(f"fleet {args.phase}: checkout SHA {actual} differs from target {args.target}")
            return 1
        if args.phase == "supervision":
            return process_proof(
                command.home,
                environ=command.config.process_environment(),
                pid=command.pid,
                target=actual,
                selection=_selection_key(args),
                require_preflight=args.require_preflight,
            )
        _ = get_field("machine_host")
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

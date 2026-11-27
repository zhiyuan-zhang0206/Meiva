# meiva:link cli/fleet_update/docs/structure.md

from __future__ import annotations

import shlex

from ..choices import FleetArgs, start_flags
from ..plan.host_commands import HOME
from ..plan.host_failures import HostFailure
from ..session import FailedError, RefusedError, Session
from .receipt import SUPERVISION_UNBOUND, DomainReceipt, receipt_from_output, require_process_proof


def _prepare_command(args: FleetArgs, target: str) -> str:
    stop_budget = args.down.stop_timeout if args.down is not None else 0
    budget = 2 * (args.start_timeout + stop_budget)
    quoted_target = shlex.quote(target)
    return (
        f"{HOME}\n"
        'W=$(mktemp -d "${TMPDIR:-/tmp}/meiva-fleet-compat.XXXXXX") || exit 1\n'
        """trap 'git -C "$S" worktree remove --force "$W" >/dev/null 2>&1 || rmdir "$W"' EXIT\n"""
        f'git worktree add -q --detach "$W" {quoted_target} || exit 1\n'
        'cd "$W" || exit 1\n'
        "test -f cli/fleet_update/probes/__main__.py || "
        '{ echo "target does not support fleet domain receipts"; exit 2; }\n'
        'py="$S/.venv/bin/python"\n'
        f'if ! git -C "$S" diff --quiet HEAD {quoted_target} -- uv.lock .python-version; then\n'
        '  UV_PROJECT_ENVIRONMENT="$W/.venv" env -u VIRTUAL_ENV uv sync -q --frozen '
        "--no-install-project --no-build-package meiva || exit 1\n"
        '  py="$W/.venv/bin/python"\n'
        "fi\n"
        'MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip env -u PYTHONPATH "$py" '
        f"-m cli.fleet_update.probes prepare {quoted_target} "
        f'--start-budget {budget} --launch-repo "$S" {start_flags(args)}'
    )


def _observe_command(args: FleetArgs, phase: str, target: str | None) -> str:
    expected = shlex.quote(target) if target is not None else '"$(git rev-parse HEAD)"'
    required = "--require-preflight " if args.half == "run" else ""
    return (
        f"{HOME}\n"
        "test -f cli/fleet_update/probes/__main__.py || "
        '{ echo "FLEET_PROBE_UNSUPPORTED"; exit 0; }\n'
        'MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -m cli.fleet_update.probes '
        f"{phase} {expected} {required}{start_flags(args)}"
    )


def _issues(receipt: DomainReceipt, *, prepare: bool) -> list[str]:
    def allowed(component: str, state: str) -> bool:
        if state == "ready" or (state == "not-installed" and component.startswith("harness/")):
            return True
        if prepare:
            return state == "selected" or (
                component in ("schema", "checkpoint") and state == "migration-pending"
            )
        return component == "preflight" and state == "legacy-unbound"

    context = (
        f"target {receipt.target}, config {receipt.config}, "
        f"schema {receipt.applied} (expected {receipt.schema})"
    )
    return [
        f"{component}: {state} [{context}]"
        for component, state in receipt.components.items()
        if not allowed(component, state)
    ]


def prepare_receipts(
    s: Session, args: FleetArgs, hosts: tuple[str, ...], target: str, *, schedules: str
) -> bool:
    if args.dry_run:
        s.say("domain compatibility: not checked (dry run does not stage mirrors)")
        return False
    command = _prepare_command(args, target)
    problems: list[str] = []
    receipts: dict[str, DomainReceipt] = {}
    for alias, output in zip(hosts, s.gather(hosts, lambda a: s.run(a, command)), strict=True):
        try:
            if isinstance(output, FailedError):
                raise output
            receipt = receipt_from_output(output, target=target)
            receipts[alias] = receipt
            problems.extend(f"{alias}: {problem}" for problem in _issues(receipt, prepare=True))
        except (FailedError, ValueError) as exc:
            problems.append(f"{alias}: domain compatibility for {target}: {exc}")
    gateway = receipts.get(args.gateway)
    if gateway is not None:
        s.say(
            f"domain stored schedules: {schedules}, target {target}, "
            f"config {gateway.config}, schema {gateway.schema}"
        )
        keep_services = args.down is not None and args.down.kept != "--keep-no-services"
        problems.extend(
            _fleet_issues(
                receipts,
                gateway=gateway,
                gateway_alias=args.gateway,
                full_wave=not keep_services,
            )
        )
    if problems:
        raise RefusedError("refused before stop: " + "; ".join(problems))
    return gateway is not None and gateway.applied != gateway.schema


def _fleet_issues(
    receipts: dict[str, DomainReceipt],
    *,
    gateway: DomainReceipt,
    gateway_alias: str,
    full_wave: bool,
) -> list[str]:
    problems: list[str] = []
    if gateway.scope != "local":
        problems.append(f"{gateway_alias}: designated gateway reported runner config scope")
    if not full_wave and gateway.applied != gateway.schema:
        problems.append(
            f"{gateway_alias}: schema migration {gateway.applied} -> {gateway.schema} "
            "requires all listed hosts and a full service stop"
        )
    runner = next(((a, row.config) for a, row in receipts.items() if row.scope == "runner"), None)
    for alias, receipt in receipts.items():
        if (receipt.schema, receipt.applied) != (gateway.schema, gateway.applied):
            problems.append(
                f"{alias}: schema version {receipt.applied}/{receipt.schema} differs from "
                f"{gateway_alias} {gateway.applied}/{gateway.schema} for {gateway.target}"
            )
        if runner is not None and receipt.scope == "runner" and receipt.config != runner[1]:
            problems.append(
                f"{alias}: runner config generation {receipt.config} differs from "
                f"{runner[0]} {runner[1]} for {gateway.target}"
            )
    return problems


def verify_receipts(
    s: Session, args: FleetArgs, hosts: tuple[str, ...], target: str | None
) -> list[HostFailure]:
    steps = tuple((host, phase) for host in hosts for phase in ("supervision", "ready"))
    outcomes = s.gather(
        steps, lambda step: s.run(step[0], _observe_command(args, step[1], target), effect=False)
    )
    failures: list[HostFailure] = []
    for (alias, phase), output in zip(steps, outcomes, strict=True):
        step = "process supervision" if phase == "supervision" else "domain readiness"
        try:
            if isinstance(output, FailedError):
                raise output
            if "FLEET_PROBE_UNSUPPORTED" in output.splitlines() and args.half == "up":
                s.say(f"WARNING: {alias}: {step} unavailable on this legacy checkout")
                continue
            if phase == "supervision":
                if SUPERVISION_UNBOUND in output.splitlines() and args.half == "up":
                    s.say(
                        f"WARNING: {alias}: process supervision has no matching preflight expectation"
                    )
                else:
                    require_process_proof(output, target=target)
            else:
                receipt = receipt_from_output(output, target=target)
                failures.extend(
                    HostFailure(alias, step, reason) for reason in _issues(receipt, prepare=False)
                )
                if receipt.components.get("preflight") == "legacy-unbound":
                    s.say(f"WARNING: {alias}: readiness has no earlier compatibility receipt")
        except (FailedError, ValueError) as exc:
            failures.append(HostFailure(alias, step, str(exc)))
    return failures

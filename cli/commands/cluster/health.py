# meiva:note
# Observe cluster health from the CLI or an OS-scheduled probe.
#
# Checks cover gateway liveness, agent population, schema, selected services, Redis relay, disk
# usage, WAL archiving, editable-install records, source integrity, and provider account health.
# Any failed check returns unhealthy.
#
# Every check runs on every round (a failing check never stops the others) and every failed
# check emits its own `health_probe_failing` event (check, failure class, message), repeated on
# every run while it stays failed. Every run also emits one `health_probe_ran` heartbeat (its
# absence is the dead-man signal) carrying the count of checks that failed; Grafana alert rules
# turn the failing stream into graded notifications (`for:` is the normal-recovery budget, then
# WARNING, then ERROR). The probe itself decides nothing about notifying.
#
# The probe never selects a release, rolls back code, or publishes known-good state. It is a
# shell around a judgment: `health_readings` observes the cluster, `plans.health_verdicts`
# decides what each check says, and this module prints and signals what it returns.

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from base import telemetry
from base.agents.context.clients import DatabaseFactory
from base.paths.home import home_checkout_error
from base.telemetry import EventPipeline
from cli.commands.cluster._provider_guard import provider_guard_config
from cli.commands.cluster.health_readings import HealthOptions, read_health
from cli.commands.cluster.plans.health_verdicts import (
    FailureClass,
    HealthRules,
    judge_health,
)
from cli.commands.data_plane.config import data_plane_config_from_boot
from cli.database.runtime import CommandRuntime

# meiva:note
# Data-volume used fraction at which the probe fails (alert-only). One line shared across the
# statvfs family: the trace auto-degrade watermark (`MEIVA_TRACE_DISK_WATERMARK=0.9`), the 312
# resource watcher's CRITICAL threshold, and the 2026-08-08 incident level. The disk the
# checkpoint/trace mirrors grow on filling is the gateway-can't-start outage class, so the
# owner should hear as the trace auto-degrade line is reached, not after (metric canon:
# `disk_usage_fraction`).
DEFAULT_DISK_USAGE_WATERMARK = 0.90

_MACOS_DATA_VOLUME = Path("/System/Volumes/Data")


# meiva:note
# Emit this run's `health_probe_failing` signal for one failed check.
#
# `check` is the stable name Grafana groups by; `failure_class` is code, environment,
# maintenance, or alert-only (a check whose failure rolling code back cannot fix). Emit never
# raises.
def _report_failing(check: str, message: str, failure_class: FailureClass) -> None:
    telemetry.emit(
        "telemetry",
        "health_probe_failing",
        level="warning",
        source="health-probe",
        attributes={
            "check": check,
            "failure_class": failure_class.value,
            "message": message,
        },
    )


# meiva:note
# Emit this run's `health_probe_ran` heartbeat; its absence means the probe stopped.
# `unhealthy_checks` counts this round's failed checks (0 = healthy).
def _report_ran(unhealthy_checks: int) -> None:
    telemetry.emit(
        "telemetry",
        "health_probe_ran",
        source="health-probe",
        attributes={"unhealthy_checks": unhealthy_checks},
    )


# meiva:note
# Return 0 for healthy, 1 for unhealthy, and 2 for a checkout refusal.
#
# Failed checks emit `health_probe_failing`; nothing here selects, rolls back or publishes a
# release. A worktree or dev checkout driving the probe is the 2026-08-07 accident (task
# #1025): worktree code misjudges schema health against prod data, so the wrong runtime is
# refused with a distinct exit code (2) and the cron log shows the refusal. A refusal never
# observed the cluster, so it is not a run and emits no heartbeat.
def run_health_probe(
    options: HealthOptions,
    *,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    if options.checkout_refusal is not None:
        print(f"health-probe refused: {options.checkout_refusal}", file=sys.stderr)
        return 2
    telemetry.init_telemetry(
        home=command.home,
        process="cli-health-probe",
        pipeline=producer(),
        machine_reader=command.machine.name,
    )
    readings = read_health(options, database_factory=database_factory, command=command)
    unhealthy_checks = 0
    for result in judge_health(readings, options.rules):
        for line in result.stdout:
            print(line)
        for line in result.stderr:
            print(line, file=sys.stderr)
        if result.failure is not None:
            unhealthy_checks += 1
            _report_failing(result.check, result.failure.message, result.failure.failure_class)
    _report_ran(unhealthy_checks)
    return 1 if unhealthy_checks else 0


# meiva:note
# What this command's round is configured by, read once from its own configuration owner and
# the host: the gateway's address, the agent floor (`MEIVA_HEALTH_PROBE_AGENT_MIN`: a test/QA
# cluster with no resident agents sets it to 0 or the check otherwise reports a permanent
# population outage), the prod checkout (every unit runs from its own `<home>/source`) and its
# allowlist, the data plane's configuration (the WAL-G configuration file is read from it and
# nowhere else), the volume the disk check measures (macOS keeps user data on its own volume) and
# whether this checkout may drive the probe at all. The user's home, the checkout and the
# platform are the invocation's, read once by the entry into `command`.
def health_options(command: CommandRuntime, *, check_schema: bool) -> HealthOptions:
    view = command.config.view
    return HealthOptions(
        gateway_url=view.gateway.gateway_url,
        rules=HealthRules(
            agent_min=view.daemon.health_probe_agent_min,
            check_schema=check_schema,
            disk_watermark=DEFAULT_DISK_USAGE_WATERMARK,
        ),
        provider=provider_guard_config(command.config),
        source_root=command.home / "source",
        editable_allowed_roots=(command.session.user_home / "Meiva",),
        data_plane=data_plane_config_from_boot(command.config),
        data_volume=_MACOS_DATA_VOLUME if command.host.platform.system == "darwin" else Path("/"),
        checkout_refusal=home_checkout_error(command.checkout, home=command.home),
    )


# meiva:note
# Report cluster health through exit status, diagnostics, and `health_probe_failing` events.
def cmd_health_probe(
    *,
    check_schema: bool,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    return run_health_probe(
        health_options(command, check_schema=check_schema),
        database_factory=database_factory,
        producer=producer,
        command=command,
    )

# meiva:note
# Load-bearing timing relationships for hosted agents and cluster maintenance.
#
# Each registered clock is safe only relative to its declared neighbours: a
# graceful exit outlasts the restart interrupt it waits for, service-readiness
# windows nest inside one another, and wedged detection allows the full exec and
# model retry budget.
# Independent HTTP deadlines remain beside their consumers.
#
# Define values in the relevant family module (deploy, stop, or schedule timing),
# register them in CLOCKS, and declare their ordering in CONSTRAINTS. Tests check
# all defaults; agent-host startup rejects operator overrides that violate the
# same relationships. The clock-lattice lint prevents unregistered timing
# constants from silently introducing competing deadlines.

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import base.deploy.progress_timeout as deploy
import base.native_process.stop_timing as stop
from base.deploy.schedule_timing import SCHEDULE_STALL_ALERT_AFTER_S
from base.host.db_authority import unit

# --- schedule supervision family ---------------------------------------------
# Value lives in base/deploy/schedule_timing.py: the gateway's schedule manager
# imports it from there directly, without the lattice module's agent/sandbox
# settings reads. The NO_PROGRESS_TIMEOUT_S ordering is declared in
# CONSTRAINTS below.


# --- wedged family: the derivation's components ------------------------------
# The wedged threshold is derived, not arbitrary: an agent holding an unconsumed
# pending inbound for this long is presumed wedged because a healthy agent's
# longest legitimate stall is one exec node (1200 s) plus the LLM retry budget.
# Historical estimate (~770 s) of the LLM retry budget under the retry config;
# not a single settings field, so stated here as the constant the wedged default
# was rounded up from (wedged.py: "exec timeout is 1200s + LLM retry budget
# ~770s ≈ 2000s, rounded up for margin").
WEDGED_AGE_SEC = 2400.0
EXEC_NODE_TIMEOUT_S = 1200.0
LLM_RETRY_BUDGET_ESTIMATE_S = 770.0


@dataclass(frozen=True)
class Clock:
    """One lattice clock: a name, a family, a value source, and why it exists."""

    family: str
    value: float | Callable[[], float]
    doc: str

    def get(self) -> float:
        return self.value() if callable(self.value) else self.value


@dataclass(frozen=True)
class Constraint:
    """One declared ordering between lattice clocks (or expressions of them).

    `lhs` / `rhs` name a clock in `CLOCKS`, optionally scaled or summed:
    `"NAME"`, `"N * NAME"`, or `"NAME + NAME"`. `kind` is one of
    `<`, `<=`, `==`, `>=`.
    """

    kind: str
    lhs: str
    rhs: str
    doc: str


CLOCKS: dict[str, Clock] = {
    # --- deploy family (values in base/deploy/progress_timeout.py) ---
    "NO_PROGRESS_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.NO_PROGRESS_TIMEOUT_S,
        "the one definition of 'this host stopped making progress'",
    ),
    "GATEWAY_PREFLIGHT_BUDGET_S": Clock(
        "deploy",
        lambda: deploy.GATEWAY_PREFLIGHT_BUDGET_S,
        "agent-runner start preflight's per-dial gateway budget",
    ),
    "ROOT_HEALTH_INTERVAL_S": Clock(
        "deploy",
        lambda: deploy.ROOT_HEALTH_INTERVAL_S,
        "the root health monitor's round interval; a stop window may not exceed it",
    ),
    "STOP_KILL_REAP_S": Clock(
        "deploy",
        lambda: deploy.STOP_KILL_REAP_S,
        "how long root waits for a SIGKILLed child to be reaped before reporting the stop as failed",
    ),
    "SERVICE_READY_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.SERVICE_READY_TIMEOUT_S,
        "how long `meiva start` waits for freshly launched services to pass liveness",
    ),
    "HOST_PACKAGE_INSTALL_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.HOST_PACKAGE_INSTALL_TIMEOUT_S,
        "how long one host-package provisioner may run during a cold start",
    ),
    "NON_CRITICAL_SERVICE_READY_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.NON_CRITICAL_SERVICE_READY_TIMEOUT_S,
        "the root health monitor startup grace for optional services",
    ),
    "DEFAULT_STARTUP_GRACE_S": Clock(
        "deploy",
        lambda: deploy.DEFAULT_STARTUP_GRACE_S,
        "the root health monitor startup grace for a unit that has no readiness tier",
    ),
    "HEALTH_RESTART_LEASE_GRACE_S": Clock(
        "deploy",
        lambda: deploy.HEALTH_RESTART_LEASE_GRACE_S,
        "how long the root health monitor holds the first restart of an outage for a "
        "live impersonation lease (task #5551)",
    ),
    # --- unit-bundle family (default in base/deploy/progress_timeout.py, cap in
    # base/host/db_authority/unit.py) ---
    "UNIT_BUNDLE_TTL_S": Clock(
        "unit-bundle",
        lambda: deploy.UNIT_BUNDLE_TTL_S,
        "a unit capability bundle's default lifetime (`issue-unit --ttl-hours`)",
    ),
    "UNIT_BUNDLE_MAX_TTL_S": Clock(
        "unit-bundle",
        lambda: unit.UNIT_BUNDLE_MAX_TTL_S,
        "the longest lifetime `issue-unit` seals into a unit capability bundle",
    ),
    # meiva:note
    # --- agent-host beat family (values in base/deploy/progress_timeout.py) ---
    "AGENT_HOST_BEAT_INTERVAL_S": Clock(
        "agent-host-beat",
        lambda: deploy.AGENT_HOST_BEAT_INTERVAL_S,
        "the agent host's ownership beat: how often it reports liveness and reaps corpses",
    ),
    "HANDOFF_SETTLE_RETRY_INTERVAL_S": Clock(
        "agent-host-beat",
        lambda: deploy.HANDOFF_SETTLE_RETRY_INTERVAL_S,
        "the first backoff of the held path's checkpoint-settle retry (geometric, x3)",
    ),
    "HANDOFF_SETTLE_RETRY_BUDGET_S": Clock(
        "agent-host-beat",
        lambda: deploy.HANDOFF_SETTLE_RETRY_BUDGET_S,
        "the whole retry budget the held path may spend settling a takeover's handoff",
    ),
    "CORPSE_REAP_GRACE_S": Clock(
        "agent-host-beat",
        lambda: deploy.CORPSE_REAP_GRACE_S,
        "how long a crash-marked idling row may sit dead before the corpse "
        "reaper stamps it terminated ('reaper')",
    ),
    # --- schedule supervision family ---
    "SCHEDULE_STALL_ALERT_AFTER_S": Clock(
        "schedule-supervision",
        SCHEDULE_STALL_ALERT_AFTER_S,
        "how long an enabled non-completed schedule may remain sessionless before alerting",
    ),
    # --- wedged family ---
    "WEDGED_AGE_SEC": Clock(
        "wedged",
        WEDGED_AGE_SEC,
        "running-agent age of an unconsumed pending inbound that presumes an agent wedged",
    ),
    "EXEC_NODE_TIMEOUT_S": Clock(
        "wedged",
        EXEC_NODE_TIMEOUT_S,
        "graph-level exec node timeout (wedged derivation component)",
    ),
    "LLM_RETRY_BUDGET_ESTIMATE_S": Clock(
        "wedged",
        LLM_RETRY_BUDGET_ESTIMATE_S,
        "historical estimate of the LLM retry budget (wedged derivation component)",
    ),
    # --- stop family (values in base/native_process/stop_timing.py) ---
    "CANCEL_UNWIND_TIMEOUT_S": Clock(
        "stop",
        lambda: stop.CANCEL_UNWIND_TIMEOUT_S,
        "cancel unwind: how long a hosted runner's cancel waits for the host to emit host_turn_uncancellable",
    ),
    "CLOCK_READ_TIMEOUT_S": Clock(
        "stop",
        lambda: stop.CLOCK_READ_TIMEOUT_S,
        "stuck-clock read: how long the cancel path may wait for one clock read",
    ),
    "HOST_GRACEFUL_EXIT_DRAIN_S": Clock(
        "stop",
        lambda: stop.HOST_GRACEFUL_EXIT_DRAIN_S,
        "graceful exit: how long a SIGTERMed agent-host waits for in-flight turns to settle",
    ),
    "RESTART_INTERRUPT_GRACE_S": Clock(
        "stop",
        lambda: stop.RESTART_INTERRUPT_GRACE_S,
        "how long a pending restart waits before it interrupts the call or code run in flight",
    ),
    "INTERRUPT_POLL_S": Clock(
        "stop",
        lambda: stop.INTERRUPT_POLL_S,
        "the interrupt watcher's poll: how soon an in-flight call notices a durable interrupt",
    ),
    "HOST_STOP_CEILING_S": Clock(
        "stop",
        lambda: stop.HOST_STOP_CEILING_S,
        "the agent-host's whole SIGTERM exit; root's TERM window for the unit derives from it",
    ),
    "OPS_DRAIN_S": Clock(
        "stop",
        lambda: stop.OPS_DRAIN_S,
        "graceful exit: how long a SIGTERMed ops server waits for admitted requests and their workers",
    ),
    "OPS_STOP_CEILING_S": Clock(
        "stop",
        lambda: stop.OPS_STOP_CEILING_S,
        "the ops server's whole SIGTERM exit; root's TERM window for the unit derives from it",
    ),
    "STOP_MARGIN_S": Clock(
        "stop",
        lambda: stop.STOP_MARGIN_S,
        "root's TERM window for a unit with a declared shutdown ceiling: ceiling plus this margin",
    ),
}


CONSTRAINTS: list[Constraint] = [
    # meiva:note
    # --- deploy family ---
    Constraint(
        "<",
        "HANDOFF_SETTLE_RETRY_BUDGET_S",
        "NO_PROGRESS_TIMEOUT_S",
        "a settle retry budget can never be what makes a host look stalled",
    ),
    Constraint(
        "<",
        "HANDOFF_SETTLE_RETRY_BUDGET_S",
        "CORPSE_REAP_GRACE_S",
        "the retry finishes long before the reaper's grace, so the two never race for the same row",
    ),
    # meiva:note
    # The floor of the same family: the retry's two backoffs (one interval, then 3x) must fit inside
    # the budget, or the third attempt is cut short before it can run and the budget no longer buys
    # the three attempts it is declared to pay for.
    Constraint(
        "<",
        "HANDOFF_SETTLE_RETRY_INTERVAL_S + 3 * HANDOFF_SETTLE_RETRY_INTERVAL_S",
        "HANDOFF_SETTLE_RETRY_BUDGET_S",
        "the two backoffs before the third attempt fit inside the budget that pays for them",
    ),
    Constraint(
        "<",
        "GATEWAY_PREFLIGHT_BUDGET_S",
        "NO_PROGRESS_TIMEOUT_S",
        "one preflight dial can never be what makes a host look stalled",
    ),
    Constraint(
        "<",
        "NON_CRITICAL_SERVICE_READY_TIMEOUT_S",
        "SERVICE_READY_TIMEOUT_S",
        "optional root-health startup grace is shorter than the core-service grace",
    ),
    Constraint(
        ">=",
        "HOST_PACKAGE_INSTALL_TIMEOUT_S",
        "SERVICE_READY_TIMEOUT_S",
        "a cold package install is a separate, earlier budget and must not fit "
        "inside the service-readiness window",
    ),
    # --- unit-bundle family ---
    Constraint(
        "<=",
        "UNIT_BUNDLE_TTL_S",
        "UNIT_BUNDLE_MAX_TTL_S",
        "the default bundle lifetime must be one `issue-unit` accepts, or issuing "
        "a bundle without `--ttl-hours` refuses",
    ),
    # --- schedule supervision family ---
    Constraint(
        "<",
        "NO_PROGRESS_TIMEOUT_S",
        "SCHEDULE_STALL_ALERT_AFTER_S",
        "the schedule-silence alert must outlive a legitimate rollout's "
        "no-progress window, so normal stop-the-world service churn stays quiet",
    ),
    Constraint(
        "<",
        "RESTART_INTERRUPT_GRACE_S + INTERRUPT_POLL_S + CANCEL_UNWIND_TIMEOUT_S",
        "HOST_GRACEFUL_EXIT_DRAIN_S",
        "a turn still running when its restart's grace ends is interrupted at the next poll "
        "and has one unwind bound to abort and flush, all inside the drain; a shorter drain "
        "would cancel turns the restart was about to interrupt",
    ),
    Constraint(
        "<",
        "HOST_GRACEFUL_EXIT_DRAIN_S + 2 * CANCEL_UNWIND_TIMEOUT_S + CLOCK_READ_TIMEOUT_S",
        "HOST_STOP_CEILING_S",
        "a drain that times out still has the scheduler's cancel unwind and clock read and the "
        "resource join inside the host's stop ceiling, with room left for the runtime, pool "
        "and health-server closes that carry no bound of their own",
    ),
    Constraint(
        "<",
        "HOST_STOP_CEILING_S + STOP_MARGIN_S",
        "ROOT_HEALTH_INTERVAL_S",
        "root's TERM window for the agent-host (its ceiling plus the margin) stays inside "
        "the health round a replacement stop runs inside, with slack rather than equality",
    ),
    Constraint(
        "<",
        "OPS_DRAIN_S",
        "OPS_STOP_CEILING_S",
        "the drain leaves room inside the ops server's stop ceiling for the pool, the event "
        "pipeline and the server closes that carry no bound of their own",
    ),
    Constraint(
        "<",
        "OPS_STOP_CEILING_S + STOP_MARGIN_S",
        "ROOT_HEALTH_INTERVAL_S",
        "root's TERM window for the ops server (its ceiling plus the margin) stays inside "
        "the health round a replacement stop runs inside, with slack rather than equality",
    ),
    # --- wedged family ---
    Constraint(
        ">=",
        "WEDGED_AGE_SEC",
        "EXEC_NODE_TIMEOUT_S + LLM_RETRY_BUDGET_ESTIMATE_S",
        "the wedged threshold must cover a healthy agent's longest legitimate "
        "stall: one exec node plus the LLM retry budget (2400 >= 1200 + 770)",
    ),
]


# meiva:note
# A snapshot of every registered clock for the lattice check. The parameters replace the three
# clocks a boot configuration moves — the wedged age, the exec node timeout and the held path's
# settle budget — so the host root checks the values it will live by, not the defaults they were
# seeded from.
def clock_values(
    wedged_age_sec: float = WEDGED_AGE_SEC,
    exec_node_timeout_s: float = EXEC_NODE_TIMEOUT_S,
    handoff_settle_budget_s: float = deploy.HANDOFF_SETTLE_RETRY_BUDGET_S,
) -> dict[str, float]:
    values = {name: clock.get() for name, clock in CLOCKS.items()}
    values["WEDGED_AGE_SEC"] = float(wedged_age_sec)
    values["EXEC_NODE_TIMEOUT_S"] = float(exec_node_timeout_s)
    values["HANDOFF_SETTLE_RETRY_BUDGET_S"] = float(handoff_settle_budget_s)
    return values


# meiva:note
# Constraint expressions contain a name, a scaled name, or a sum of those terms.
def _resolve(expr: str, values: Mapping[str, float]) -> float:
    if " + " in expr:
        return sum(_resolve(part, values) for part in expr.split(" + "))
    if " * " in expr:
        n, name = expr.split(" * ", 1)
        return float(n) * values[name]
    return values[expr]


def validate_clock_lattice(values: Mapping[str, float]) -> list[str]:
    if values.keys() != CLOCKS.keys():
        raise ValueError("clock snapshot must contain exactly the registered clock names")
    failures: list[str] = []
    for c in CONSTRAINTS:
        left, right = _resolve(c.lhs, values), _resolve(c.rhs, values)
        holds = {
            "<": left < right,
            "<=": left <= right,
            "==": left == right,
            ">=": left >= right,
        }[c.kind]
        if not holds:
            failures.append(f"{c.lhs} {c.kind} {c.rhs}: {left} vs {right} — {c.doc}")
    return failures


class ClockLatticeError(RuntimeError):
    """Raised when live clock values violate a declared lattice ordering."""


def assert_clock_lattice(values: Mapping[str, float]) -> None:
    """Fail fast on any lattice violation. Called at agent-host startup so an
    operator env override that inverts a load-bearing ordering dies loudly on
    the box that lives by these clocks, never silently mid-incident."""
    failures = validate_clock_lattice(values)
    if failures:
        raise ClockLatticeError("clock lattice violated:\n  " + "\n  ".join(failures))

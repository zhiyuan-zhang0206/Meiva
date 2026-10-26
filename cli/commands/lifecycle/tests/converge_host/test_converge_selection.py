# meiva:note
# Which converge steps run, why the others do not, and the loop that runs them.

from dataclasses import replace
from pathlib import Path

import pytest

from base.config import ConfigBoot
from cli.commands.converge.incomplete import read_incomplete, record_incomplete
from cli.commands.converge.plans.step_selection import RunStep, SkipStep, StepScope, plan_steps
from cli.commands.converge.spec import ALL_ROLES, ConvergeCtx, ConvergeStep
from cli.commands.lifecycle import converge_host
from cli.database.runtime import CommandRuntime


def _scope(
    name: str,
    *,
    roles: frozenset[str] = ALL_ROLES,
    requires_unit_config: bool = False,
    host_global: bool = False,
    services: frozenset[str] = frozenset(),
) -> StepScope:
    return StepScope(name, roles, requires_unit_config, host_global, services)


# meiva:note
# --- the selection decision ---------------------------------------------------------


def test_preparation_filters_service_consumers_but_keeps_shared_host_steps() -> None:
    plan = plan_steps(
        (
            _scope("shared"),
            _scope("backend", services=frozenset({"loki", "grafana"})),
            _scope("collector", services=frozenset({"otel-collector"})),
        ),
        roles=frozenset({"gateway"}),
        services=frozenset({"loki"}),
        prod_install=True,
    )
    assert plan == (
        RunStep("shared"),
        RunStep("backend"),
        SkipStep("collector", "consumer service not selected"),
    )


def test_an_unconfigured_unit_runs_wiring_and_defers_unit_state() -> None:
    plan = plan_steps(
        (_scope("wiring"), _scope("unit", requires_unit_config=True)),
        roles=None,
        services=frozenset(),
        prod_install=True,
    )
    assert plan == (
        RunStep("wiring"),
        SkipStep("unit", "unit not configured yet; initialize with meiva start"),
    )


def test_a_step_runs_when_the_unit_carries_any_capability_it_is_scoped_to() -> None:
    steps = (
        _scope("gateway-only", roles=frozenset({"gateway"})),
        _scope("runner-only", roles=frozenset({"agent-runner"})),
        _scope("both"),
    )
    runner = plan_steps(
        steps, roles=frozenset({"agent-runner"}), services=frozenset(), prod_install=True
    )
    single_box = plan_steps(
        steps,
        roles=frozenset({"gateway", "agent-runner"}),
        services=frozenset(),
        prod_install=True,
    )
    assert runner == (
        SkipStep("gateway-only", "roles agent-runner"),
        RunStep("runner-only"),
        RunStep("both"),
    )
    assert single_box == (RunStep("gateway-only"), RunStep("runner-only"), RunStep("both"))


# meiva:note
# Dev clusters must leave the production symlink and shell wiring alone.
@pytest.mark.parametrize("prod_install", [True, False])
def test_host_global_wiring_belongs_to_the_prod_install_only(prod_install: bool) -> None:
    plan = plan_steps(
        (_scope("hostwide", host_global=True), _scope("percluster")),
        roles=frozenset({"gateway"}),
        services=frozenset(),
        prod_install=prod_install,
    )
    expected_hostwide = (
        RunStep("hostwide")
        if prod_install
        else SkipStep("hostwide", "dev cluster/worktree — host-global wiring is prod-install only")
    )
    assert plan == (expected_hostwide, RunStep("percluster"))


def test_the_consumer_service_check_decides_before_the_host_and_unit_checks() -> None:
    plan = plan_steps(
        (
            _scope(
                "collector", host_global=True, requires_unit_config=True, services=frozenset({"x"})
            ),
        ),
        roles=None,
        services=frozenset(),
        prod_install=False,
    )
    assert plan == (SkipStep("collector", "consumer service not selected"),)


# meiva:note
# --- the loop that runs the decisions ------------------------------------------------


def _noop_steps(*names: str) -> tuple[ConvergeStep, ...]:
    def noop(_ctx: ConvergeCtx) -> None:
        return None

    return tuple(ConvergeStep(name, noop) for name in names)


def test_the_loop_runs_the_steps_that_were_decided_in_order_and_reports_each(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = _noop_steps("first", "second")
    skipped = ConvergeStep("gateway-only", first.apply, roles=frozenset({"gateway"}))

    converge_host.run_converge(ctx, (first, skipped, second), prod_install=False)

    assert capsys.readouterr().out.splitlines() == [
        "",
        "→ converge host",
        "  ✓ first",
        "  · gateway-only: skipped (roles agent-runner)",
        "  ✓ second",
    ]


def test_the_first_failing_step_stops_the_loop_and_reraises(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    (after,) = _noop_steps("after")

    def boom(_ctx: ConvergeCtx) -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        converge_host.run_converge(ctx, (ConvergeStep("boom", boom), after), prod_install=False)

    captured = capsys.readouterr()
    assert "✗ boom: nope" in captured.err
    assert "after" not in captured.out, "no step runs after the first failure"


def test_a_failed_step_is_recorded_for_the_prod_install_and_a_clean_run_clears_it(
    ctx: ConvergeCtx,
) -> None:
    def boom(_ctx: ConvergeCtx) -> None:
        raise RuntimeError("the login keychain is not unlocked")

    with pytest.raises(RuntimeError):
        converge_host.run_converge(ctx, (ConvergeStep("keychain", boom),), prod_install=True)
    recorded = read_incomplete(ctx.meiva_home)
    assert recorded is not None and recorded.step == "keychain"

    converge_host.run_converge(ctx, (), prod_install=True)
    assert read_incomplete(ctx.meiva_home) is None


def test_a_dev_cluster_converge_never_writes_or_clears_the_marker(ctx: ConvergeCtx) -> None:
    def boom(_ctx: ConvergeCtx) -> None:
        raise RuntimeError("the login keychain is not unlocked")

    with pytest.raises(RuntimeError):
        converge_host.run_converge(ctx, (ConvergeStep("keychain", boom),), prod_install=False)
    assert read_incomplete(ctx.meiva_home) is None

    record_incomplete(ctx.meiva_home, step="keychain", error="locked", now=ctx.clock.now())
    converge_host.run_converge(ctx, (), prod_install=False)
    assert read_incomplete(ctx.meiva_home) is not None


# meiva:note
# --- the composition point ------------------------------------------------------------


def test_converge_builds_one_lazy_context_per_operation_from_the_command(
    tmp_path: Path, command_runtime: CommandRuntime, ctx: ConvergeCtx
) -> None:
    command = replace(command_runtime, home=tmp_path / "operated", config=ConfigBoot())
    observed: list[ConvergeCtx] = []

    def observe(step_ctx: ConvergeCtx) -> None:
        observed.append(step_ctx)

    steps = (ConvergeStep("first", observe), ConvergeStep("second", observe))
    for _ in range(2):
        converge_host.converge_host(
            tmp_path,
            None,
            steps=steps,
            services=frozenset({"gateway"}),
            database_factory=ctx.database_factory,
            producer=ctx.producer,
            command=command,
        )

    assert observed[0] is observed[1]
    assert observed[2] is observed[3]
    for seen in (observed[0], observed[2]):
        assert seen.meiva_home == command.home
        assert seen.config is command.config
        assert seen.machine is command.machine
        assert seen.host is command.host
        assert seen.services == frozenset({"gateway"})
    assert not command.config.prepared, "context construction must not deliver configuration"

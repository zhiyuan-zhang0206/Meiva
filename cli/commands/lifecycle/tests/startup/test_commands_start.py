# meiva:note
# The refusals and early exits of `meiva start`, before anything is prepared or launched.
#
# Each runs the whole start shell against a real home (a short temporary directory) and the suite's
# Postgres; the decisions themselves are asserted on the plans in `../start_plans`.

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli import start_identity
from cli.commands import _setup
from cli.commands.lifecycle import stand_down
from cli.commands.lifecycle import start as start_package
from cli.commands.lifecycle.tests.startup.start_harness import StartRun
from cli.database.runtime import CommandRuntime
from cli.startup.tests.birth_facts import birth_facts

# meiva:note
# ── the stand-down record ────────────────────────────────────────────────────


# meiva:note
# The boot job exists for an accidental death, not to undo an operator's stop (task #5493):
# it ends at once, successfully, and leaves the record that tells the next boot the same.
def test_the_boot_job_leaves_a_home_down_that_an_operator_stopped(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    stand_down.record(run.home, now=datetime.now(UTC), until=None, reason="operator stop")

    assert run.start(boot_attempt=True) == 0

    assert stand_down.read(run.home) is not None
    out = capsys.readouterr().out
    assert "the boot job leaves this home stopped" in out
    assert "roles =" not in out, "nothing past the stand-down gate ran"


# meiva:note
# With the window over, the boot job starts: here it reaches the identity refusal of a unit
# that declared nothing, which an ended stand-down does not skip.
def test_the_boot_job_proceeds_once_a_declared_window_has_ended(start_run: StartRun) -> None:
    run = start_run
    now = datetime.now(UTC)
    stand_down.record(run.home, now=now, until=now - timedelta(minutes=5), reason="window")

    assert run.start(boot_attempt=True) == 1

    assert stand_down.read(run.home) is not None, "the boot job never clears the record"


def test_an_explicit_start_ends_a_stand_down_before_anything_else_runs(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    stand_down.record(run.home, now=datetime.now(UTC), until=None, reason="operator stop")

    # meiva:note
    # the undeclared unit is refused after the gate
    assert run.start() == 1

    assert stand_down.read(run.home) is None
    assert "stand-down cleared by an explicit start" in capsys.readouterr().out


# meiva:note
# ── the home and the checkout ────────────────────────────────────────────────


def test_a_home_being_destroyed_refuses_to_start(start_run: StartRun) -> None:
    run = start_run
    run.declare(gateway=False, agent_runner=True)
    (run.home / "destroy-intent.json").write_text("{}")

    with pytest.raises(RuntimeError, match="home is being destroyed or detached"):
        run.start()


# meiva:note
# The prod home must not launch from a disposable development checkout.
def test_a_home_with_its_own_source_checkout_refuses_any_other_checkout(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    run.declare(gateway=False, agent_runner=True)
    (run.home / "source").mkdir()

    assert run.start() == 1

    err = capsys.readouterr().err
    assert "is not the checkout of the home it would act on" in err
    assert f"{run.home}/source" in err


# meiva:note
# ── the unit's identity ──────────────────────────────────────────────────────


# meiva:note
# Serve-capability is the entry to the role-aware filter; when none is declared other fields
# cannot be judged for relevance, so the error lists only the capability declarations.
def test_a_unit_that_declares_no_capability_is_told_only_the_capability_flags(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    config = run.command.config
    config.set_field("machine_name", "before-failed-setup")
    run.command.machine.reset()
    assert run.command.machine.name() == "before-failed-setup"
    for field in ("machine_serve_gateway", "machine_serve_agent_runner"):
        config.set_field(field, None)
    config.set_field("machine_name", "")
    config.set_field("gateway_url", "")

    assert run.start() == 1

    err = capsys.readouterr().err
    assert "recorded setup is incomplete" in err
    assert "MEIVA_MACHINE_SERVE_GATEWAY" in err
    assert "MEIVA_MACHINE_SERVE_AGENT_RUNNER" in err
    assert "MEIVA_MACHINE_NAME" not in err
    assert "`meiva init`" in err, "a start never takes identity input"
    assert run.command.machine.name() == "before-failed-setup", "a failed setup resets nothing"


@pytest.mark.parametrize("capability", ["gateway", "agent-runner"])
def test_a_capable_unit_missing_its_fields_is_told_which_keys_to_set(
    start_run: StartRun, capsys: pytest.CaptureFixture[str], capability: str
) -> None:
    run = start_run
    run.declare(gateway=capability == "gateway", agent_runner=capability == "agent-runner")
    run.command.config.set_field("machine_name", "")
    run.command.config.set_field("gateway_url", "")

    assert run.start() == 1

    err = capsys.readouterr().err
    assert "MEIVA_MACHINE_NAME" in err
    assert "MEIVA_GATEWAY_URL" in err


# meiva:note
# The capability set is fixed when a home is initialized: a start whose resolved
# capabilities differ from the intent's refuses before any effect.
def test_capabilities_that_differ_from_the_ones_init_recorded_refuse_before_any_effect(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            run.home,
            run.runtime.code_root,
            frozenset({"agent-runner"}),
            {"MEIVA_MACHINE_NAME": "t", "MEIVA_GATEWAY_URL": "http://127.0.0.1:1"},
            occupied_ports=frozenset(),
            birth=birth_facts(),
        )
    )
    run.declare(gateway=True, agent_runner=False)

    assert run.start() == 1

    assert "differ from the ones `meiva init` recorded" in capsys.readouterr().err


# meiva:note
# Setup that now resolves refreshes the identity cached before it, in this invocation only.
def test_a_start_refreshes_only_its_invocations_machine(start_run: StartRun) -> None:
    from cli.database.runtime import CommandRuntime

    run = start_run
    other = CommandRuntime.create(run.home)
    other.config.set_field("machine_name", "other-invocation")
    run.command.config.set_field("machine_name", "before-setup")
    run.command.machine.reset()
    assert run.command.machine.name() == "before-setup"
    assert other.machine.name() == "other-invocation"
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            run.home,
            run.runtime.code_root,
            frozenset({"agent-runner"}),
            {"MEIVA_MACHINE_NAME": "t", "MEIVA_GATEWAY_URL": "http://127.0.0.1:1"},
            occupied_ports=frozenset(),
            birth=birth_facts(),
        )
    )
    run.declare(gateway=True, agent_runner=False)
    run.command.config.set_field("machine_name", "repaired-name")

    # meiva:note
    # refused on its capabilities, after setup resolved
    assert run.start() == 1

    assert run.command.machine.name() == "repaired-name"
    assert other.machine.name() == "other-invocation"


# meiva:note
# The configuration value (the home's `.env`); a validator gates it, a legacy `machine_name`
# file is not read, and resolution never writes the home (`meiva init` owns its identity).
def test_setup_field_resolves_from_the_configuration_only_and_writes_nothing(
    unit_home: Path, command_runtime: CommandRuntime
) -> None:
    checked: list[str] = []
    field = _setup.SetupField(
        name="machine_name",
        env_var="MEIVA_MACHINE_NAME",
        hint="<name>",
        validator=checked.append,
    )
    command_runtime.config.set_field("machine_name", "")
    (unit_home / "machine_name").write_text("from-file\n")
    before = sorted(path.name for path in unit_home.iterdir())

    assert _setup.resolve_setup_field(field, config=command_runtime.config) is None
    # meiva:note
    # nothing written
    assert sorted(path.name for path in unit_home.iterdir()) == before

    command_runtime.config.set_field("machine_name", " from-env ")
    assert _setup.resolve_setup_field(field, config=command_runtime.config) == "from-env"
    assert checked == ["from-env"]


# meiva:note
# Handing systemd the verified root belongs to the dispatch around the start (`cli.start_intent`),
# after the start returned zero: nothing in the start itself publishes it.
def test_the_start_leaves_boot_publication_to_the_public_dispatch() -> None:
    package = Path(inspect.getfile(start_package)).parent
    sources = [path.read_text() for path in package.glob("*.py")]

    assert sources
    assert all("complete_boot_start" not in source for source in sources)

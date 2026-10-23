# meiva:note
# Where the cluster's installed extensions land: after the schema check, never as a converge step.
#
# The order inside a cold start (migrations, schema check, adopt, materialize) is the plan asserted
# in `../start_plans/test_cold_start_plan.py`. What stays here are the contracts of the code around
# it: materialization is not a converge step, and the standalone `meiva converge` lands extensions
# the same way.

from __future__ import annotations

import inspect


def test_materialization_is_not_a_converge_step() -> None:
    from cli.commands.lifecycle.converge_host import CONVERGE_STEPS

    offenders = [s.name for s in CONVERGE_STEPS if "extension" in s.name.lower()]
    assert not offenders, (
        f"{offenders} is a converge STEP, but converge runs before this cluster's "
        "Postgres is started and before migrations apply. Call it from the start "
        "sequence after the schema check instead — see cli/commands/extensions/materialize.py:"
        "materialize_cluster_extensions."
    )


def test_standalone_converge_materializes_too() -> None:
    from cli.commands.lifecycle.converge_host import cmd_converge

    assert (
        "materialize_cluster_extensions(database_factory=database_factory, skills_root=skills_root)"
        in inspect.getsource(cmd_converge)
    )


def test_the_materializer_lives_beside_its_siblings() -> None:
    from cli.commands.extensions import materialize

    assert hasattr(materialize, "materialize_cluster_extensions")


def test_standalone_converge_adopts_too() -> None:
    from cli.commands.lifecycle.converge_host import cmd_converge

    source = inspect.getsource(cmd_converge)
    assert "adopt_local_extensions(" in source
    assert "machine_name=command.machine.name" in source
    assert "skills_root=skills_root" in source

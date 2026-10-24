# meiva:note
# Where the cluster's installed extensions land: after the schema check, never as a converge step.
#
# The order inside a cold start (migrations, schema check, adopt, materialize) is the plan asserted
# in `../start_plans/test_cold_start_plan.py`. What stays here are the contracts of the code around
# it: materialization is not a converge step. That the standalone `meiva converge` lands and adopts
# extensions the same way is asserted against a real cluster registry in
# `../converge_host/test_cmd_converge.py`.

from __future__ import annotations


def test_materialization_is_not_a_converge_step() -> None:
    from cli.commands.lifecycle.converge_host import CONVERGE_STEPS

    offenders = [s.name for s in CONVERGE_STEPS if "extension" in s.name.lower()]
    assert not offenders, (
        f"{offenders} is a converge STEP, but converge runs before this cluster's "
        "Postgres is started and before migrations apply. Call it from the start "
        "sequence after the schema check instead — see cli/commands/extensions/materialize.py:"
        "materialize_cluster_extensions."
    )

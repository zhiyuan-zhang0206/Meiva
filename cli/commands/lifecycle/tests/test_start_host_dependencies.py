from cli.commands.data_plane import host_dependencies
from cli.commands.lifecycle.start import cold_start_steps


def test_host_dependency_step_runs_first() -> None:
    steps = cold_start_steps()
    assert steps[0].name == "host dependency packages"
    assert steps[0].apply is host_dependencies.ensure_host_dependencies_step

# meiva:note
# The root's command line and the keeper's seed, as values.

from __future__ import annotations

from pathlib import Path

from cli.commands.lifecycle.root_control.launch_plan import keeper_seed, plan_root_launch


def launch() -> tuple[Path, Path]:
    return Path("/home/run/meiva-root"), Path("/home/run/meiva-root/manifests.json")


def test_the_command_line_is_the_roots_module_with_its_run_dir_manifests_and_wiring() -> None:
    run_dir, manifests = launch()

    plan = plan_root_launch(
        interpreter=Path("/src/.venv/bin/python"),
        cwd=Path("/src"),
        run_dir=run_dir,
        manifests=manifests,
    )

    assert plan.argv == (
        "/src/.venv/bin/python",
        "-m",
        "services.supervision.meiva_root",
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests),
        "--wiring",
        "services.supervision.meiva_root_glue.glue:build_wiring",
    )
    assert plan.cwd == Path("/src")
    assert plan.manifests == manifests


def test_the_root_logs_under_its_run_dir() -> None:
    run_dir, manifests = launch()

    plan = plan_root_launch(
        interpreter=Path("/py"), cwd=Path("/src"), run_dir=run_dir, manifests=manifests
    )

    assert plan.stdout_log == run_dir / "root.stdout.log"
    assert plan.stderr_log == run_dir / "root.stderr.log"


def test_the_keeper_seed_carries_the_same_command_with_absolute_paths_and_the_env() -> None:
    run_dir, manifests = launch()
    plan = plan_root_launch(
        interpreter=Path("/py"), cwd=Path("/src"), run_dir=run_dir, manifests=manifests
    )

    seed = keeper_seed(plan, {"MEIVA_HOME": "/home", "PATH": "/bin"})

    assert seed == {
        "argv": list(plan.argv),
        "cwd": "/src",
        "run_dir": str(run_dir),
        "stdout": str(plan.stdout_log),
        "stderr": str(plan.stderr_log),
        "env": {"MEIVA_HOME": "/home", "PATH": "/bin"},
    }

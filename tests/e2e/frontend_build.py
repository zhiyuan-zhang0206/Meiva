from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from tests.e2e.process_support import proc_log_tail

# meiva:note
# How the e2e suite builds and serves `ui/web` for one browser session.

# meiva:note
# Both commands run inside a per-session copy of `ui/web` whose `node_modules` is a symlink back
# to the workspace tree (`ui/web/node_modules`): the dependencies are the workspace's, and this
# module is the one place that names how pnpm is invoked against them.
PNPM = ("pnpm",)


def pnpm_run(script: str, *arguments: str) -> list[str]:
    return [*PNPM, "run", script, *arguments]


# meiva:note
# Copy `source` to `build_dir`, link its `node_modules`, and run `next build` there. A failing
# build raises with the last lines of its own output: that log lives on the runner, which outlives
# neither the job nor the reader, so the reason has to travel in the exception.
def build_frontend(source: Path, build_dir: Path, *, env: Mapping[str, str], log: Path) -> None:
    if build_dir.exists():
        shutil.rmtree(build_dir)
    shutil.copytree(
        source,
        build_dir,
        ignore=shutil.ignore_patterns(
            "node_modules", ".next", ".builds", "coverage", "*.log", "tsconfig.tsbuildinfo"
        ),
    )
    # meiva:note
    # The symlink is relative and stays inside `ui/web`, which is what lets Turbopack treat the
    # layout as a hoisted monorepo lookup instead of refusing a dependency outside its root.
    (build_dir / "node_modules").symlink_to(Path("../../node_modules"))

    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as stream:
        result = subprocess.run(  # noqa: S603
            pnpm_run("build"),
            cwd=str(build_dir),
            env=dict(env),
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"pnpm run build failed (exit {result.returncode}); last lines of {log}:\n"
            + proc_log_tail(str(log))
        )

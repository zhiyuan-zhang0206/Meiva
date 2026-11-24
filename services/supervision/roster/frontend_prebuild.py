from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from base.cluster.derive import fe_build_env_vars
from base.cluster.frontend_build import ensure_build
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.boundary.options import options
from services.supervision.roster.composition import FrontendBuildInputs, frontend_build_inputs


@dataclass(frozen=True)
class _Options:
    stage: str
    serving: str


_FRONTEND = Path("ui") / "web"


def prebuild(
    *,
    stage: Path,
    serving: Path,
    roles: frozenset[str],
    inputs: FrontendBuildInputs,
    copy_tree: Callable[[Path, Path], None],
    build: Callable[[Path], int],
    environ: MutableMapping[str, str],
) -> int:
    if "gateway" not in roles:
        print("frontend prebuild: this host serves no frontend")  # noqa: T201
        return 0
    serving_frontend, stage_frontend = serving / _FRONTEND, stage / _FRONTEND
    copy_tree(serving_frontend / "node_modules", stage_frontend / "node_modules")
    warm_cache = serving_frontend / ".next" / "cache"
    if warm_cache.is_dir():
        (stage_frontend / ".next").mkdir(exist_ok=True)
        copy_tree(warm_cache, stage_frontend / ".next" / "cache")
    environ.update(
        fe_build_env_vars(gateway_port=inputs.gateway_port, browser_origins=inputs.browser_origins)
    )
    return build(stage_frontend)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="services.supervision.roster.frontend_prebuild")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--serving", required=True)
    chosen = options(parser.parse_args(argv), _Options)
    inputs = frontend_build_inputs()
    clone = ["-Rc"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]

    def copy_tree(source: Path, dest: Path) -> None:
        subprocess.run(["cp", *clone, str(source), str(dest)], check=True)  # noqa: S603

    def build(frontend: Path) -> int:
        return ensure_build(
            frontend,
            gateway_port=inputs.gateway_port,
            browser_origins=inputs.browser_origins,
        )

    return prebuild(
        stage=Path(chosen.stage),
        serving=Path(chosen.serving),
        roles=ConfiguredMachineIdentity(ConfigBoot()).role(),
        inputs=inputs,
        copy_tree=copy_tree,
        build=build,
        environ=os.environ,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

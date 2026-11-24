from __future__ import annotations

import shutil
from pathlib import Path

from base.cluster.derive import fe_build_env, fe_build_env_vars
from services.supervision.roster.composition import FrontendBuildInputs
from services.supervision.roster.frontend_prebuild import prebuild

_INPUTS = FrontendBuildInputs(8800, ("https://a.example", "https://b.example"))


def _serving_tree(root: Path) -> Path:
    frontend = root / "ui" / "web"
    (frontend / "node_modules" / "pkg").mkdir(parents=True)
    (frontend / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
    (frontend / ".next" / "cache").mkdir(parents=True)
    (frontend / ".next" / "cache" / "warm").write_text("warm\n")
    return root


def _copy(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest, symlinks=True)


def test_a_gateway_host_stages_a_real_copy_and_builds_in_the_stage(tmp_path: Path) -> None:
    serving, stage = _serving_tree(tmp_path / "serving"), tmp_path / "stage"
    (stage / "ui" / "web").mkdir(parents=True)
    built: list[tuple[Path, dict[str, str]]] = []
    environ: dict[str, str] = {}

    def build(frontend: Path) -> int:
        built.append((frontend, dict(environ)))
        return 0

    code = prebuild(
        stage=stage,
        serving=serving,
        roles=frozenset({"gateway", "agent-runner"}),
        inputs=_INPUTS,
        copy_tree=_copy,
        build=build,
        environ=environ,
    )

    assert code == 0
    assert built == [
        (
            stage / "ui" / "web",
            {
                "NEXT_PUBLIC_GATEWAY_PORT": "8800",
                "NEXT_PUBLIC_BROWSER_ORIGINS": "https://a.example,https://b.example",
            },
        )
    ]
    copy = stage / "ui" / "web" / "node_modules"
    assert not copy.is_symlink()
    assert (copy / "pkg" / "index.js").read_text() == "module.exports = 1\n"
    assert (stage / "ui" / "web" / ".next" / "cache" / "warm").read_text() == "warm\n"
    assert (serving / "ui" / "web" / "node_modules" / "pkg" / "index.js").is_file()


def test_a_failed_build_is_the_exit_status(tmp_path: Path) -> None:
    serving, stage = _serving_tree(tmp_path / "serving"), tmp_path / "stage"
    (stage / "ui" / "web").mkdir(parents=True)
    code = prebuild(
        stage=stage,
        serving=serving,
        roles=frozenset({"gateway"}),
        inputs=_INPUTS,
        copy_tree=_copy,
        build=lambda _frontend: 3,
        environ={},
    )
    assert code == 3


def test_a_host_without_the_gateway_builds_nothing(tmp_path: Path) -> None:
    built: list[Path] = []

    def build(frontend: Path) -> int:
        built.append(frontend)
        return 0

    stage = tmp_path / "stage"
    code = prebuild(
        stage=stage,
        serving=tmp_path,
        roles=frozenset({"agent-runner"}),
        inputs=_INPUTS,
        copy_tree=_copy,
        build=build,
        environ={},
    )
    assert code == 0 and built == [] and not stage.exists()


def test_the_shell_prefix_and_the_environment_are_one_rendering() -> None:
    for port, origins in [(8000, ()), (8800, ("https://a.example", "https://b.example"))]:
        env = fe_build_env_vars(gateway_port=port, browser_origins=origins)
        assert env["NEXT_PUBLIC_GATEWAY_PORT"] == str(port)
        assert ("NEXT_PUBLIC_BROWSER_ORIGINS" in env) == bool(origins)
        rendered = fe_build_env(gateway_port=port, browser_origins=origins)
        assert rendered.split()[0] == f"NEXT_PUBLIC_GATEWAY_PORT={port}"
        assert env.get("NEXT_PUBLIC_BROWSER_ORIGINS", "") == ",".join(origins)

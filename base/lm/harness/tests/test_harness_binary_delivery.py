import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.lm.harness import wiring
from base.lm.harness.codex import CodexConfig
from base.lm.harness.codex.session import resolved_binary as codex_binary


def _install(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


# meiva:note
# The session spawns the CLI it resolved, not the bare name: a harness child is handed an
# allowlisted environment with no PATH, so `codex` would resolve against the OS default and
# fail with a bare "No such file or directory" from a real spawn (#5491).
# meiva:link base/lm/harness/docs/harness.md
def test_a_codex_session_resolves_the_cli_it_spawns(tmp_path: Path) -> None:
    installed = _install(tmp_path / "bin", "codex")
    empty = tmp_path / "empty"
    empty.mkdir()

    resolved = codex_binary(
        CodexConfig(cwd=tmp_path),
        {"HOME": str(tmp_path), "PATH": str(empty)},
        bin_dirs=(str(installed.parent),),
    )

    assert resolved == str(installed)


# meiva:note
# The session environment is the delivery point that rebuilds PATH: the daemon child
# environment it starts from is an allowlist that carries none, so without this the CLI
# and every tool it runs are unresolvable even when the host can see them (#5491).
# meiva:link base/lm/harness/docs/harness.md
def test_a_session_environment_carries_the_dirs_a_harness_installs_into(
    tmp_path: Path,
) -> None:
    entries = wiring.session_environ({"HOME": str(tmp_path)})["PATH"].split(os.pathsep)

    assert str(Path.home() / ".local/bin") in entries
    assert "/usr/bin" in entries


def test_a_given_environment_is_never_dropped(tmp_path: Path) -> None:
    environment = wiring.session_environ({"HOME": str(tmp_path), "MEIVA_HOME": str(tmp_path)})

    assert environment["MEIVA_HOME"] == str(tmp_path)
    assert Path(environment["MEIVA_PYTHON"]).name == "meiva_python.sh"
    assert environment["MEIVA_PYTHON_EXECUTABLE"] == sys.executable
    assert environment["SHELL"].endswith("noninteractive_bash.sh")
    assert Path(environment["SHELL"]).is_file()


@pytest.mark.parametrize("script_mode", [False, True])
def test_meiva_python_imports_installed_sdk_from_a_foreign_checkout(
    tmp_path: Path, script_mode: bool
) -> None:
    shadow = tmp_path / "meiva"
    shadow.mkdir()
    (shadow / "__init__.py").write_text('raise RuntimeError("shadowed")\n')
    code = 'import meiva, os; print(meiva.__file__, "PYTHONSAFEPATH" in os.environ)'
    script = tmp_path / "probe.py"
    script.write_text(code)
    environment = wiring.session_environ(
        {"HOME": str(tmp_path), "MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"}
    )
    environment["PATH"] = str(tmp_path)
    args = [str(script)] if script_mode else ["-c", code]

    result = subprocess.run(  # noqa: S603
        [environment["MEIVA_PYTHON"], *args],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    source = Path(__file__).resolve().parents[4] / "meiva" / "__init__.py"
    assert result.stdout.strip() == f"{source} False"


def test_meiva_python_runs_modules_from_the_pinned_environment(tmp_path: Path) -> None:
    (tmp_path / "json.py").write_text('raise RuntimeError("shadowed")\n')
    environment = wiring.session_environ({"HOME": str(tmp_path)})
    result = subprocess.run(  # noqa: S603
        [environment["MEIVA_PYTHON"], "-m", "json.tool"],
        cwd=tmp_path,
        env=environment,
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "{}"

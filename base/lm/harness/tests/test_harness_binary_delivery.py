import os
from pathlib import Path

from base.lm.harness import wiring
from base.lm.harness.codex import CodexConfig


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
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def test_a_codex_session_resolves_the_cli_it_spawns(tmp_path: Path) -> None:
    installed = _install(tmp_path / "bin", "codex")
    empty = tmp_path / "empty"
    empty.mkdir()

    resolved = CodexConfig(cwd=tmp_path).resolved_binary(
        {"HOME": str(tmp_path), "PATH": str(empty)},
        bin_dirs=(str(installed.parent),),
    )

    assert resolved == str(installed)


# meiva:note
# The session environment is the delivery point that rebuilds PATH: the daemon child
# environment it starts from is an allowlist that carries none, so without this the CLI
# and every tool it runs are unresolvable even when the host can see them (#5491).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def test_a_session_environment_carries_the_dirs_a_harness_installs_into(
    tmp_path: Path,
) -> None:
    entries = wiring.session_environ({"HOME": str(tmp_path)})["PATH"].split(os.pathsep)

    assert str(Path.home() / ".local/bin") in entries
    assert "/usr/bin" in entries


def test_a_given_environment_is_never_dropped(tmp_path: Path) -> None:
    environment = wiring.session_environ({"HOME": str(tmp_path), "MEIVA_HOME": str(tmp_path)})

    assert environment["MEIVA_HOME"] == str(tmp_path)

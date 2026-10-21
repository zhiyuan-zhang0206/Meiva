import os
import subprocess
from contextlib import chdir
from pathlib import Path

import pytest

from base.host.env.harness_binaries import (
    HARNESS_BIN_DIRS,
    HarnessBinaryNotFoundError,
    harness_child_path,
    locate_harness_binary,
    resolve_harness_binary,
    with_harness_path,
)


def _install(root: Path, relative: str, name: str = "agy") -> Path:
    path = root / relative / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_a_cli_outside_path_resolves_through_the_known_install_dirs(tmp_path: Path) -> None:
    installed = _install(tmp_path, ".local/bin")

    found, on_path = locate_harness_binary("agy", home=tmp_path, path_env="/usr/bin:/bin")

    assert found == str(installed)
    assert on_path is False


def test_path_wins_when_it_already_holds_the_program(tmp_path: Path) -> None:
    installed = _install(tmp_path, ".local/bin")

    found, on_path = locate_harness_binary("agy", home=tmp_path, path_env=str(installed.parent))

    assert found == str(installed)
    assert on_path is True


@pytest.mark.parametrize("symlink", [False, True])
def test_a_relative_path_cli_runs_from_another_working_directory(
    tmp_path: Path, symlink: bool
) -> None:
    installed = _install(tmp_path, "bin")
    installed.write_text('#!/bin/sh\nprintf \'%s\\n\' "$0" "$PWD"\n')
    executable = installed
    if symlink:
        executable = tmp_path / "links" / installed.name
        executable.parent.mkdir()
        executable.symlink_to(Path("../bin") / installed.name)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with chdir(tmp_path):
        resolved = resolve_harness_binary(
            installed.name,
            path_env=str(executable.parent.relative_to(tmp_path)),
            bin_dirs=(),
        )

    # meiva:note
    # Execute only the test-owned stub, from a different directory, to verify the
    # path still selects the installed file rather than merely looking absolute.
    completed = subprocess.run(  # noqa: S603
        [resolved], cwd=workspace, capture_output=True, text=True, check=True, timeout=5
    )

    assert resolved == str(executable)
    assert completed.stdout.splitlines() == [str(executable), str(workspace)]


def test_a_configured_relative_path_is_returned_unchanged(tmp_path: Path) -> None:
    configured = _install(tmp_path, "vendor/bin")
    relative = str(configured.relative_to(tmp_path))

    with chdir(tmp_path):
        assert resolve_harness_binary(relative, path_env="", bin_dirs=()) == relative


def test_a_missing_cli_names_path_and_every_directory_it_searched(tmp_path: Path) -> None:
    with pytest.raises(HarnessBinaryNotFoundError) as raised:
        resolve_harness_binary("no-such-harness-cli", home=tmp_path, path_env="/usr/bin:/bin")

    message = str(raised.value)
    assert "/usr/bin:/bin" in message
    for entry in HARNESS_BIN_DIRS:
        expected = entry if Path(entry).is_absolute() else str(tmp_path / entry)
        assert expected in message


def test_a_configured_absolute_path_is_never_redirected(tmp_path: Path) -> None:
    elsewhere = _install(tmp_path, ".local/bin")
    configured = _install(tmp_path, "vendor/bin")

    assert resolve_harness_binary(str(configured), home=tmp_path, path_env=None) == str(configured)

    configured.chmod(0o644)
    with pytest.raises(HarnessBinaryNotFoundError, match=str(configured)):
        resolve_harness_binary(str(configured), home=tmp_path, path_env=str(elsewhere.parent))


def test_the_child_path_keeps_the_host_entries_first_and_drops_repeats(tmp_path: Path) -> None:
    host_path = os.pathsep.join(["/usr/bin", str(tmp_path / ".local/bin")])

    entries = harness_child_path(path_env=host_path, home=tmp_path).split(os.pathsep)

    assert entries[0] == "/usr/bin"
    assert entries.count(str(tmp_path / ".local/bin")) == 1
    assert entries.count("/usr/bin") == 1
    assert entries[-3:] == ["/bin", "/usr/sbin", "/sbin"]


def test_with_harness_path_replaces_the_inherited_path_and_keeps_the_other_keys(
    tmp_path: Path,
) -> None:
    environ = {"MEIVA_HOME": str(tmp_path / "home"), "HOME": str(tmp_path)}

    session = with_harness_path(environ, home=tmp_path)

    assert session["MEIVA_HOME"] == str(tmp_path / "home")
    assert session["HOME"] == str(tmp_path)
    assert str(tmp_path / ".local/bin") in session["PATH"].split(os.pathsep)

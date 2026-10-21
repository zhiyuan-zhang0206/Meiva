import os
from pathlib import Path

import pytest

from base.host.env.harness_binaries import (
    HARNESS_BIN_DIRS,
    HarnessBinaryNotFoundError,
    HarnessPathIncompleteError,
    harness_child_path,
    harness_unit_path,
    locate_harness_binary,
    require_harness_paths,
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


# meiva:note
# A unit that writes its own narrow PATH puts every service under it back on the OS
# default, where `claude` and `agy` do not exist; the refusal names the unit and the
# directories it dropped (#5491, #5495).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def test_a_unit_path_that_omits_a_harness_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HarnessPathIncompleteError) as raised:
        require_harness_paths("/usr/bin:/bin", home=tmp_path, what="the test unit PATH")

    message = str(raised.value)
    assert "the test unit PATH" in message
    assert str(tmp_path / ".local/bin") in message
    assert str(tmp_path / ".npm-global/bin") in message


def test_the_unit_builder_always_covers_the_table(tmp_path: Path) -> None:
    built = harness_unit_path(leading=["/vendor/bin"], home=tmp_path, what="the test unit PATH")

    entries = built.split(os.pathsep)
    assert entries[0] == "/vendor/bin"
    assert require_harness_paths(built, home=tmp_path, what="the test unit PATH") == built

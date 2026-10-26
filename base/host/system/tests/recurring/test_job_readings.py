# meiva:note
# Read only admitted executable locations; roots stay owned by the caller.

from pathlib import Path

from base.host.env.harness_binaries import require_harness_paths
from base.host.system.job_readings import read_launchd_path, read_meiva_binary
from base.host.system.tests.fake_commands import fake_host


def test_checkout_binary_precedes_the_calling_process_path(tmp_path: Path) -> None:
    binary = tmp_path / ".venv" / "bin" / "meiva"
    binary.parent.mkdir(parents=True)
    binary.touch()
    host = fake_host(programs={"meiva": "/another/checkout/meiva"})
    assert read_meiva_binary(tmp_path, host=host) == str(binary)


def test_install_without_a_checkout_binary_uses_path_or_the_candidate(tmp_path: Path) -> None:
    host = fake_host(programs={"meiva": "/installed/bin/meiva"})
    assert read_meiva_binary(tmp_path, host=host) == "/installed/bin/meiva"
    assert read_meiva_binary(tmp_path, host=fake_host()) == str(tmp_path / ".venv/bin/meiva")


def test_launchd_path_uses_the_explicit_users_harness_locations(tmp_path: Path) -> None:
    result = read_launchd_path(
        "/checkout/.venv/bin/meiva",
        user_home=tmp_path,
        host=fake_host(programs={"brew": "/custom/brew/bin/brew"}),
    )
    parts = result.split(":")
    assert parts[:2] == ["/checkout/.venv/bin", "/custom/brew/bin"]
    assert str(tmp_path / ".local/bin") in parts
    assert str(tmp_path / ".bun/bin") in parts
    assert len(parts) == len(set(parts))


def test_launchd_path_includes_brew_bin(tmp_path: Path) -> None:
    path = read_launchd_path(
        "/Users/x/.local/bin/meiva",
        user_home=tmp_path,
        host=fake_host(programs={"brew": "/opt/homebrew/bin/brew"}),
    )
    assert "/opt/homebrew/bin" in path
    assert "/Users/x/.local/bin" in path
    assert "/usr/bin" in path


def test_launchd_path_falls_back_without_brew(tmp_path: Path) -> None:
    path = read_launchd_path("/Users/x/.local/bin/meiva", user_home=tmp_path, host=fake_host())
    assert "/opt/homebrew/bin" in path
    assert "/usr/local/bin" in path


# meiva:note
# The runner's launchd job starts every harness, and `claude` / `agy` install into the
# user-local bins; a job that reaches only the venv's own dir and brew cannot start them
# (#5491). Pin the user dirs even when `meiva` itself lives under the brew prefix.
def test_launchd_path_reaches_the_user_install_dirs(tmp_path: Path) -> None:
    entries = read_launchd_path("/x/bin/meiva", user_home=tmp_path, host=fake_host()).split(":")

    assert str(tmp_path / ".local/bin") in entries
    assert str(tmp_path / ".npm-global/bin") in entries


# meiva:note
# The producer contract: whatever this job writes must itself pass the check every unit
# PATH has to pass, so a future edit that narrows the list fails here rather than at the
# first harness spawn (#5495).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def test_the_launchd_path_passes_the_unit_check(tmp_path: Path) -> None:
    path = read_launchd_path(
        "/x/bin/meiva",
        user_home=tmp_path,
        host=fake_host(programs={"brew": "/opt/homebrew/bin/brew"}),
    )

    assert require_harness_paths(path, home=tmp_path, what="the launchd job PATH") == path


def test_launchd_path_deduplicates(tmp_path: Path) -> None:
    host = fake_host(programs={"brew": "/opt/homebrew/bin/brew"})
    assert (
        read_launchd_path("/opt/homebrew/bin/meiva", user_home=tmp_path, host=host)
        .split(":")
        .count("/opt/homebrew/bin")
        == 1
    )

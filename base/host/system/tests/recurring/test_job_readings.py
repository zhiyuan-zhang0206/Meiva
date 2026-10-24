# meiva:note
# Read only admitted executable locations; roots stay owned by the caller.

from pathlib import Path

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

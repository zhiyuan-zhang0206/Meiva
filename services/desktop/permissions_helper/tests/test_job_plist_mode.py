# meiva:note
# The installed job definition's mode belongs to the installation, not to the shell that ran it.
#
# `retirement_plist` refuses to act on a definition another principal could rewrite (the
# group/other write bits), so an installer that leaves the mode to the process umask installs a
# helper its own home's `meiva stop` then refuses to retire. These cases run on every host: the
# writer and the check are plain file operations, and the umask is the only variable the product
# must not depend on.

from __future__ import annotations

import os
import plistlib
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from base.host.system.cron.mechanics import LAUNCHD_PLIST_MODE, write_launchd_plist
from services.desktop.permissions_helper import launchd_job as jobs


# meiva:note
# Run one write under a chosen umask and restore the process-wide value afterwards.
@contextmanager
def _umask(value: int) -> Generator[None]:
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def _body(home: Path, *, executable: str = "/private/test/MeivaPermissionsHelper") -> bytes:
    return plistlib.dumps(
        {
            "Label": jobs.helper_job_label(home),
            "KeepAlive": {"SuccessfulExit": False},
            "ProgramArguments": [executable],
            "EnvironmentVariables": {
                "MEIVA_PERMISSIONS_HELPER_SOCKET": str(home / "run/permissions-helper.9223.sock"),
                "MEIVA_PERMISSIONS_HELPER_ROOT_SEED": str(home / "run/meiva-root/seed.json"),
            },
        }
    )


@pytest.mark.parametrize("umask", [0o022, 0o002, 0o077])
def test_the_written_definition_carries_the_installation_mode_whatever_the_umask(
    tmp_path: Path, umask: int
) -> None:
    path = tmp_path / f"job-{umask:o}.plist"

    with _umask(umask):
        write_launchd_plist(path, b"<plist/>")

    assert stat.S_IMODE(path.stat().st_mode) == LAUNCHD_PLIST_MODE
    assert LAUNCHD_PLIST_MODE & 0o022 == 0


@pytest.mark.parametrize("umask", [0o022, 0o002])
# meiva:note
# The mode the writer installs is the mode retirement demands: the two facts are one, and this is
# the case that would catch them drifting apart.
def test_a_written_definition_satisfies_the_retirement_ownership_check(
    tmp_path: Path, umask: int
) -> None:
    home = tmp_path.resolve()
    path = home / "job.plist"

    with _umask(umask):
        write_launchd_plist(path, _body(home))
        original, executable = jobs.retirement_plist(
            path, home, home / "run" / "permissions-helper.9223.sock", uid=os.getuid()
        )

    assert original == path.read_bytes()
    assert executable == "/private/test/MeivaPermissionsHelper"


# meiva:note
# The check the writer satisfies is still a check: a 0664 definition is refused by name, so the
# fix above cannot have been achieved by widening the refusal.
def test_a_definition_writable_by_another_principal_is_refused(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    path = home / "job.plist"
    write_launchd_plist(path, _body(home))
    path.chmod(0o664)

    with pytest.raises(RuntimeError, match="owned immutable input"):
        jobs.retirement_plist(
            path, home, home / "run" / "permissions-helper.9223.sock", uid=os.getuid()
        )


# meiva:note
# An installation that predates the explicit mode left 0664 behind. Retirement repairs the mode of a
# definition this user owns and reads it; it never touches contents, and it leaves a definition it
# does not own alone so the refusal still decides that case.
def test_a_definition_installed_before_the_explicit_mode_is_repaired(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    path = home / "job.plist"
    write_launchd_plist(path, _body(home))
    path.chmod(0o664)

    repair = jobs.repair_job_plist_mode(path, uid=os.getuid())

    assert repair is not None and "0664" in repair and "0644" in repair
    assert stat.S_IMODE(path.stat().st_mode) == LAUNCHD_PLIST_MODE
    assert path.read_bytes() == _body(home)
    jobs.retirement_plist(
        path, home, home / "run" / "permissions-helper.9223.sock", uid=os.getuid()
    )


def test_the_repair_never_touches_a_definition_this_user_does_not_own(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    path = home / "job.plist"
    write_launchd_plist(path, _body(home))
    path.chmod(0o664)

    assert jobs.repair_job_plist_mode(path, uid=os.getuid() + 1) is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o664
    with pytest.raises(RuntimeError, match="owned immutable input"):
        jobs.retirement_plist(
            path, home, home / "run" / "permissions-helper.9223.sock", uid=os.getuid() + 1
        )


# meiva:note
# A symlink is not a definition this repair rewrites: it is left for the check, which refuses it.
def test_a_definition_that_is_not_ours_to_repair_is_left_for_the_check(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    real = home / "real.plist"
    write_launchd_plist(real, _body(home))
    real.chmod(0o664)
    link = home / "job.plist"
    link.symlink_to(real)

    assert jobs.repair_job_plist_mode(link, uid=os.getuid()) is None
    assert stat.S_IMODE(real.stat().st_mode) == 0o664

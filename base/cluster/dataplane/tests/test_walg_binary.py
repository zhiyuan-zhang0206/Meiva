"""The pinned WAL-G binary: verified before it is installed, one stable path, Linux x86_64 only."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
from pathlib import Path

import pytest

from base.cluster.dataplane import walg_binary
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener

_FAKE_WALG = b'#!/bin/sh\necho "wal-g version v3.0.9 deadbeef 2026.08.20 PostgreSQL"\n'


@dataclasses.dataclass
class PinnedWalgEnv:
    home: Path
    artifacts: dict[str, tuple[str, str]]
    opener: FakeUrlOpener

    @property
    def fetched(self) -> list[str]:
        return self.opener.urls


@pytest.fixture
def pinned(tmp_path: Path) -> PinnedWalgEnv:
    """A home under tmp, a Linux x86_64 platform, and a pin for the fake artifact."""
    opener = FakeUrlOpener()
    opener.on("https://github.com/wal-g/", Answer(body=_FAKE_WALG), repeat=True)
    return PinnedWalgEnv(
        home=tmp_path / "home",
        artifacts={
            "linux-x86_64": ("wal-g-fake", hashlib.sha256(_FAKE_WALG).hexdigest()),
        },
        opener=opener,
    )


def test_only_linux_x86_64_is_pinned() -> None:
    assert set(walg_binary.WALG_ARTIFACTS) == {"linux-x86_64"}


@pytest.mark.parametrize(("system", "machine"), [("Darwin", "arm64"), ("Linux", "aarch64")])
def test_other_platforms_fail_fast(pinned: PinnedWalgEnv, system: str, machine: str) -> None:
    with pytest.raises(RuntimeError, match="no pinned wal-g artifact for"):
        walg_binary.ensure_walg_binary(
            environ=os.environ,
            home=pinned.home,
            system=system,
            machine=machine,
            artifacts=pinned.artifacts,
            open_url=pinned.opener,
        )

    assert pinned.fetched == []
    assert "no pinned wal-g artifact" in (
        walg_binary.installed_problem(
            home=pinned.home,
            system=system,
            machine=machine,
            artifacts=pinned.artifacts,
        )
        or ""
    )


def test_install_downloads_verifies_and_publishes_an_executable(pinned: PinnedWalgEnv) -> None:
    path = walg_binary.ensure_walg_binary(
        environ=os.environ,
        home=pinned.home,
        system="Linux",
        machine="x86_64",
        artifacts=pinned.artifacts,
        open_url=pinned.opener,
    )

    assert path == walg_binary.walg_path(pinned.home)
    assert path.name == "wal-g" and path.parent.name == "walg"
    assert pinned.fetched == [
        f"https://github.com/wal-g/wal-g/releases/download/{walg_binary.WALG_VERSION}/wal-g-fake"
    ]
    assert path.read_bytes() == _FAKE_WALG
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert (
        walg_binary.installed_problem(
            home=pinned.home,
            system="Linux",
            machine="x86_64",
            artifacts=pinned.artifacts,
        )
        is None
    )
    assert [p.name for p in path.parent.iterdir()] == ["wal-g"], "no staging file is left behind"


def test_install_is_idempotent(pinned: PinnedWalgEnv) -> None:
    walg_binary.ensure_walg_binary(
        environ=os.environ,
        home=pinned.home,
        system="Linux",
        machine="x86_64",
        artifacts=pinned.artifacts,
        open_url=pinned.opener,
    )
    walg_binary.ensure_walg_binary(
        environ=os.environ,
        home=pinned.home,
        system="Linux",
        machine="x86_64",
        artifacts=pinned.artifacts,
        open_url=pinned.opener,
    )

    assert len(pinned.fetched) == 1


def test_checksum_mismatch_installs_nothing(
    pinned: PinnedWalgEnv,
) -> None:
    bad_opener = FakeUrlOpener()
    bad_opener.on("https://github.com/wal-g/", Answer(body=_FAKE_WALG + b"tampered"))

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        walg_binary.ensure_walg_binary(
            environ=os.environ,
            home=pinned.home,
            system="Linux",
            machine="x86_64",
            artifacts=pinned.artifacts,
            open_url=bad_opener,
        )

    assert not walg_binary.walg_path(pinned.home).exists()


def test_wrong_version_never_displaces_the_installed_binary(
    pinned: PinnedWalgEnv,
) -> None:
    walg_binary.ensure_walg_binary(
        environ=os.environ,
        home=pinned.home,
        system="Linux",
        machine="x86_64",
        artifacts=pinned.artifacts,
        open_url=pinned.opener,
    )
    stale = b'#!/bin/sh\necho "wal-g version v0.0.1"\n'
    stale_opener = FakeUrlOpener()
    stale_opener.on("https://github.com/wal-g/", Answer(body=stale))
    stale_artifacts = {
        "linux-x86_64": ("wal-g-fake", hashlib.sha256(stale).hexdigest()),
    }

    with pytest.raises(RuntimeError, match=r"does not report v3\.0\.9"):
        walg_binary.ensure_walg_binary(
            environ=os.environ,
            home=pinned.home,
            system="Linux",
            machine="x86_64",
            artifacts=stale_artifacts,
            open_url=stale_opener,
        )

    assert walg_binary.walg_path(pinned.home).read_bytes() == _FAKE_WALG
    assert [p.name for p in walg_binary.walg_path(pinned.home).parent.iterdir()] == ["wal-g"]


def test_installed_problem_names_each_defect(pinned: PinnedWalgEnv) -> None:
    assert "is not installed" in (
        walg_binary.installed_problem(
            home=pinned.home,
            system="Linux",
            machine="x86_64",
            artifacts=pinned.artifacts,
        )
        or ""
    )

    path = walg_binary.ensure_walg_binary(
        environ=os.environ,
        home=pinned.home,
        system="Linux",
        machine="x86_64",
        artifacts=pinned.artifacts,
        open_url=pinned.opener,
    )
    path.write_bytes(_FAKE_WALG + b"# edited by hand\n")
    assert "is not the pinned v3.0.9 build" in (
        walg_binary.installed_problem(
            home=pinned.home,
            system="Linux",
            machine="x86_64",
            artifacts=pinned.artifacts,
        )
        or ""
    )

    path.write_bytes(_FAKE_WALG)
    path.chmod(0o644)
    assert "is not executable" in (
        walg_binary.installed_problem(
            home=pinned.home,
            system="Linux",
            machine="x86_64",
            artifacts=pinned.artifacts,
        )
        or ""
    )

    path.chmod(0o755)
    assert (
        walg_binary.installed_problem(
            home=pinned.home,
            system="Linux",
            machine="x86_64",
            artifacts=pinned.artifacts,
        )
        is None
    )
    assert os.access(path, os.X_OK)


# meiva:note
# The probe is the one place this module starts a process, so it is where an ambient environment
# would leak back in. The fake binary refuses to report its version unless it was started with the
# environment the caller stated: restoring `daemon_process_env()` inside `_require_version` fails
# this case, which is the mutation check for the read this change removed.
_CALLER_MARKED_WALG = (
    b"#!/bin/sh\n"
    b'[ "$LC_ALL" = "from-the-caller" ] || '
    b'{ echo "the probe ran without the caller environment" >&2; exit 3; }\n'
    b'echo "wal-g version v3.0.9 deadbeef 2026.08.20 PostgreSQL"\n'
)


def test_the_version_probe_runs_with_the_environment_its_caller_states(tmp_path: Path) -> None:
    opener = FakeUrlOpener()
    opener.on("https://github.com/wal-g/", Answer(body=_CALLER_MARKED_WALG), repeat=True)
    artifacts = {
        "linux-x86_64": ("wal-g-fake", hashlib.sha256(_CALLER_MARKED_WALG).hexdigest()),
    }

    path = walg_binary.ensure_walg_binary(
        home=tmp_path / "home",
        system="Linux",
        machine="x86_64",
        environ={"LC_ALL": "from-the-caller"},
        artifacts=artifacts,
        open_url=opener,
    )

    assert path.exists()

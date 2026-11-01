# meiva:note
# `ensure_frontend_deps` unit tests -- lockfile-drift reinstall logic.
#
# Regression guard: after the fleet update pulls a `pnpm-lock.yaml` with new dependencies,
# it must re-run `pnpm install --frozen-lockfile`; otherwise `pnpm run build` fails immediately
# due to missing deps, the frontend session exits, and port 3000 refuses connections.
#
# `pnpm install --frozen-lockfile` is a stand-in executable the test writes, run as a real
# subprocess -- no real install, only verifying "when to install / when to skip".


from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from cli.commands import _repo

_EMPTY_LOCK = "lockfileVersion: '9.0'\n\nimporters:\n  .: {}\n"
_DEP_LOCK = (
    "lockfileVersion: '9.0'\n\n"
    "importers:\n"
    "  .:\n"
    "    dependencies:\n"
    "      '@scope/dep':\n"
    "        specifier: ^1.0.0\n"
    "        version: 1.0.0(react@19.3.0)\n"
)


# meiva:note
# A repo skeleton with ui/web/pnpm-lock.yaml holding `lock_body` and package.json holding
# `pkg_body` (the drift check compares their direct dependency versions).
def _make_repo(tmp_path: Path, lock_body: str = _EMPTY_LOCK, pkg_body: str = "{}") -> Path:
    fe = tmp_path / "ui" / "web"
    fe.mkdir(parents=True)
    (fe / "pnpm-lock.yaml").write_text(lock_body)
    (fe / "package.json").write_text(pkg_body)
    return tmp_path


# meiva:note
# A stand-in `pnpm` that behaves like `pnpm install --frozen-lockfile`'s observable effects:
# it records the directory and PATH it ran with, and populates `node_modules/.modules.yaml`.
_FAKE_PNPM = """#!/bin/sh
printf '%s\\n' "$PWD" >> "$FAKE_PNPM_LOG"
printf '%s\\n' "$PATH" > "$FAKE_PNPM_LOG.path"
mkdir -p node_modules
printf 'hoistPattern:\\n  - \"*\"\\n' > node_modules/.modules.yaml
test "$1" = install && test "$2" = --frozen-lockfile
"""

_ENVIRONMENT = {"PATH": "/usr/bin:/bin"}


@dataclass(frozen=True)
class FakePnpm:
    argv: tuple[str, ...]
    log: Path

    # meiva:note
    # The directories `pnpm install --frozen-lockfile` ran in, in order — empty means skipped.
    def installs(self) -> list[Path]:
        if not self.log.exists():
            return []
        return [Path(line) for line in self.log.read_text().splitlines()]

    def seen_path(self) -> str:
        return Path(f"{self.log}.path").read_text().strip()

    def ensure(self, repo: Path, environ: Mapping[str, str] = _ENVIRONMENT) -> None:
        _repo.ensure_frontend_deps(
            repo, environ={**environ, "FAKE_PNPM_LOG": str(self.log)}, install_argv=self.argv
        )

    def forget(self) -> None:
        self.log.unlink(missing_ok=True)


@pytest.fixture
def fake_pnpm_install(tmp_path: Path) -> FakePnpm:
    """A real subprocess that mimics `pnpm install --frozen-lockfile`."""
    tool = tmp_path / "bin" / "pnpm"
    tool.parent.mkdir()
    tool.write_text(_FAKE_PNPM)
    tool.chmod(0o700)
    return FakePnpm(argv=(str(tool), "install", "--frozen-lockfile"), log=tmp_path / "pnpm.log")


def test_installs_when_node_modules_missing(tmp_path: Path, fake_pnpm_install: FakePnpm) -> None:
    repo = _make_repo(tmp_path / "repo")
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == [(repo / "ui" / "web").resolve()]
    assert (repo / "ui" / "web" / "node_modules" / ".meiva-lock-hash").is_file()


# meiva:note
# `meiva start` must find pnpm without a shell profile adding Homebrew's bin.
def test_pnpm_install_injects_toolchain_path_from_a_minimal_non_login_shell(
    tmp_path: Path, fake_pnpm_install: FakePnpm
) -> None:
    repo = _make_repo(tmp_path / "repo")

    fake_pnpm_install.ensure(repo, {"PATH": "/usr/bin:/bin"})

    assert fake_pnpm_install.seen_path().split(":")[:4] == [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_skips_when_stamp_matches_lockfile(tmp_path: Path, fake_pnpm_install: FakePnpm) -> None:
    repo = _make_repo(tmp_path / "repo")
    fake_pnpm_install.ensure(repo)
    fake_pnpm_install.forget()
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == []


def test_reinstalls_when_lockfile_changed(tmp_path: Path, fake_pnpm_install: FakePnpm) -> None:
    repo = _make_repo(tmp_path / "repo")
    fake_pnpm_install.ensure(repo)
    fake_pnpm_install.forget()
    (repo / "ui" / "web" / "pnpm-lock.yaml").write_text(_DEP_LOCK)
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == [(repo / "ui" / "web").resolve()]


# meiva:note
# Legacy install (node_modules without `.meiva-lock-hash`) → reinstall once so the stamp is written.
def test_reinstalls_when_node_modules_exists_without_stamp(
    tmp_path: Path, fake_pnpm_install: FakePnpm
) -> None:
    repo = _make_repo(tmp_path / "repo")
    (repo / "ui" / "web" / "node_modules").mkdir()
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == [(repo / "ui" / "web").resolve()]


# meiva:note
# A foreign `npm install` writes `.package-lock.json` or wipes `.modules.yaml` while leaving
# `pnpm-lock.yaml` and `.meiva-lock-hash` untouched; neither state may be trusted.
def test_reinstalls_when_foreign_package_manager_marker_present(
    tmp_path: Path, fake_pnpm_install: FakePnpm
) -> None:
    repo = _make_repo(tmp_path / "repo")
    fake_pnpm_install.ensure(repo)
    fake_pnpm_install.forget()
    (repo / "ui" / "web" / "node_modules" / ".package-lock.json").write_text("{}")
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == [(repo / "ui" / "web").resolve()]


def test_reinstalls_when_pnpm_modules_metadata_missing(
    tmp_path: Path, fake_pnpm_install: FakePnpm
) -> None:
    repo = _make_repo(tmp_path / "repo")
    fake_pnpm_install.ensure(repo)
    fake_pnpm_install.forget()
    (repo / "ui" / "web" / "node_modules" / ".modules.yaml").unlink()
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == [(repo / "ui" / "web").resolve()]


def test_reinstalls_when_installed_version_drifts_from_lockfile(
    tmp_path: Path, fake_pnpm_install: FakePnpm
) -> None:
    repo = _make_repo(
        tmp_path / "repo",
        _DEP_LOCK,
        '{"dependencies": {"@scope/dep": "^1.0.0"}}',
    )
    fake_pnpm_install.ensure(repo)
    fake_pnpm_install.forget()
    installed = repo / "ui" / "web" / "node_modules" / "@scope" / "dep"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text('{"version": "2.0.0"}')
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == [(repo / "ui" / "web").resolve()]


def test_skips_when_installed_versions_match_lockfile(
    tmp_path: Path, fake_pnpm_install: FakePnpm
) -> None:
    repo = _make_repo(
        tmp_path / "repo",
        _DEP_LOCK,
        '{"dependencies": {"@scope/dep": "^1.0.0"}}',
    )
    fake_pnpm_install.ensure(repo)
    fake_pnpm_install.forget()
    installed = repo / "ui" / "web" / "node_modules" / "@scope" / "dep"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text('{"version": "1.0.0"}')
    fake_pnpm_install.ensure(repo)
    assert fake_pnpm_install.installs() == []

# meiva:note
# `ensure_frontend_deps` unit tests -- lockfile-drift reinstall logic.
#
# Regression guard: after the fleet update pulls a package-lock.json with new dependencies,
# it must re-run `npm ci`; otherwise `npm run build` fails immediately due to missing
# deps, the frontend session exits, and port 3000 refuses connections (root cause
# of the 2026-06-07 prod outage).
#
# `npm ci` is a stand-in executable the test writes, run as a real subprocess -- no real
# install, only verifying "when to install / when to skip".


from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from cli.commands import _repo


# meiva:note
# A repo skeleton with ui/web/package-lock.json holding `lock_body` and package.json holding
# `pkg_body` (the drift check compares their direct dependency versions).
def _make_repo(tmp_path: Path, lock_body: str, pkg_body: str = "{}") -> Path:
    fe = tmp_path / "ui" / "web"
    fe.mkdir(parents=True)
    (fe / "package-lock.json").write_text(lock_body)
    (fe / "package.json").write_text(pkg_body)
    return tmp_path


# meiva:note
# A stand-in `npm` that behaves like `npm ci`'s two observable effects: it records the directory
# and the PATH it ran with, and it recreates node_modules (a real `npm ci` wipes and recreates it).
_FAKE_NPM = """#!/bin/sh
printf '%s\\n' "$PWD" >> "$FAKE_NPM_LOG"
printf '%s\\n' "$PATH" > "$FAKE_NPM_LOG.path"
mkdir -p node_modules
test "$1" = ci
"""

_ENVIRONMENT = {"PATH": "/usr/bin:/bin"}


@dataclass(frozen=True)
class FakeNpm:
    argv: tuple[str, ...]
    log: Path

    # meiva:note
    # The directories `npm ci` ran in, in order — empty means the install was skipped.
    def installs(self) -> list[Path]:
        if not self.log.exists():
            return []
        return [Path(line) for line in self.log.read_text().splitlines()]

    def seen_path(self) -> str:
        return Path(f"{self.log}.path").read_text().strip()

    def ensure(self, repo: Path, environ: Mapping[str, str] = _ENVIRONMENT) -> None:
        _repo.ensure_frontend_deps(
            repo, environ={**environ, "FAKE_NPM_LOG": str(self.log)}, install_argv=self.argv
        )

    def forget(self) -> None:
        self.log.unlink(missing_ok=True)


@pytest.fixture
def fake_npm_ci(tmp_path: Path) -> FakeNpm:
    """A real subprocess that mimics `npm ci` by creating node_modules."""
    tool = tmp_path / "bin" / "npm"
    tool.parent.mkdir()
    tool.write_text(_FAKE_NPM)
    tool.chmod(0o700)
    return FakeNpm(argv=(str(tool), "ci"), log=tmp_path / "npm.log")


def test_installs_when_node_modules_missing(tmp_path: Path, fake_npm_ci: FakeNpm) -> None:
    repo = _make_repo(tmp_path / "repo", '{"lock": 1}')
    fake_npm_ci.ensure(repo)
    assert fake_npm_ci.installs() == [(repo / "ui" / "web").resolve()]
    # stamp written so the next call is a noop
    assert (repo / "ui" / "web" / "node_modules" / ".meiva-lock-hash").is_file()


# meiva:note
# `meiva start` must find npm without a shell profile adding Homebrew's bin.
def test_npm_ci_injects_toolchain_path_from_a_minimal_non_login_shell(
    tmp_path: Path, fake_npm_ci: FakeNpm
) -> None:
    repo = _make_repo(tmp_path / "repo", '{"lock": 1}')

    fake_npm_ci.ensure(repo, {"PATH": "/usr/bin:/bin"})

    assert fake_npm_ci.seen_path().split(":")[:4] == [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_skips_when_stamp_matches_lockfile(tmp_path: Path, fake_npm_ci: FakeNpm) -> None:
    repo = _make_repo(tmp_path / "repo", '{"lock": 1}')
    fake_npm_ci.ensure(repo)  # first install writes the stamp
    fake_npm_ci.forget()
    fake_npm_ci.ensure(repo)  # lockfile unchanged → skip
    assert fake_npm_ci.installs() == []


def test_reinstalls_when_lockfile_changed(tmp_path: Path, fake_npm_ci: FakeNpm) -> None:
    repo = _make_repo(tmp_path / "repo", '{"lock": 1}')
    fake_npm_ci.ensure(repo)  # installs against lock v1
    fake_npm_ci.forget()
    # The fleet update pulls a lockfile that added a dependency
    (repo / "ui" / "web" / "package-lock.json").write_text('{"lock": 2, "added": "dep"}')
    fake_npm_ci.ensure(repo)
    assert fake_npm_ci.installs() == [(repo / "ui" / "web").resolve()]


# meiva:note
# Legacy install (node_modules from before this stamp existed) → reinstall
# once so the stamp gets written; a bare node_modules is no longer trusted.
def test_reinstalls_when_node_modules_exists_without_stamp(
    tmp_path: Path, fake_npm_ci: FakeNpm
) -> None:
    repo = _make_repo(tmp_path / "repo", '{"lock": 1}')
    (repo / "ui" / "web" / "node_modules").mkdir()  # exists, but no .meiva-lock-hash
    fake_npm_ci.ensure(repo)
    assert fake_npm_ci.installs() == [(repo / "ui" / "web").resolve()]


# meiva:note
# A pnpm install ignores package-lock.json and leaves the stamp matching —
# the node_modules it produced must not be trusted (task #2654: radix drifted
# ^1.2.10 → 1.2.18 under the converged stamp and shipped a minifier-emptied
# function to prod).
def test_reinstalls_when_foreign_package_manager_marker_present(
    tmp_path: Path, fake_npm_ci: FakeNpm
) -> None:
    repo = _make_repo(tmp_path / "repo", '{"lock": 1}')
    fake_npm_ci.ensure(repo)  # npm ci, writes the stamp
    fake_npm_ci.forget()
    (repo / "ui" / "web" / "node_modules" / ".pnpm").mkdir(parents=True)
    fake_npm_ci.ensure(repo)
    assert fake_npm_ci.installs() == [(repo / "ui" / "web").resolve()]


# meiva:note
# `npm install <pkg>@x --no-save` leaves no foreign marker and no lockfile
# change; the version check is the layer that still fires.
def test_reinstalls_when_installed_version_drifts_from_lockfile(
    tmp_path: Path, fake_npm_ci: FakeNpm
) -> None:
    repo = _make_repo(
        tmp_path / "repo",
        '{"lock": 1, "packages": {"node_modules/dep": {"version": "1.0.0"}}}',
        '{"dependencies": {"dep": "^1.0.0"}}',
    )
    fake_npm_ci.ensure(repo)
    fake_npm_ci.forget()
    installed = repo / "ui" / "web" / "node_modules" / "dep"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text('{"version": "2.0.0"}')
    fake_npm_ci.ensure(repo)
    assert fake_npm_ci.installs() == [(repo / "ui" / "web").resolve()]


# meiva:note
# The stamp-matched path verifies versions before trusting node_modules —
# matching versions must keep the skip.
def test_skips_when_installed_versions_match_lockfile(tmp_path: Path, fake_npm_ci: FakeNpm) -> None:
    repo = _make_repo(
        tmp_path / "repo",
        '{"lock": 1, "packages": {"node_modules/dep": {"version": "1.0.0"}}}',
        '{"dependencies": {"dep": "^1.0.0"}}',
    )
    fake_npm_ci.ensure(repo)
    fake_npm_ci.forget()
    installed = repo / "ui" / "web" / "node_modules" / "dep"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text('{"version": "1.0.0"}')
    fake_npm_ci.ensure(repo)
    assert fake_npm_ci.installs() == []

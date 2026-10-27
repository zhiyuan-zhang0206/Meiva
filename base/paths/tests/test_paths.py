"""base/paths/__init__.py unit tests — MEIVA_HOME resolution + path helpers + first-access mkdir."""

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from base import paths
from base.paths.home import default_home, is_default_home, is_default_home_of, is_prod_install


def test_meiva_home_reads_settings(unit_home: Path):
    assert paths.meiva_home() == unit_home


def test_meiva_home_auto_mkdirs(unit_home: Path):
    unit_home.rmdir()
    assert not unit_home.exists()
    paths.meiva_home()
    assert unit_home.is_dir()


def test_meiva_home_repairs_owner_only_mode(unit_home: Path):
    """The unit root cannot retain permissions inherited from a lax umask."""
    unit_home.chmod(0o755)

    assert paths.meiva_home() == unit_home
    assert unit_home.stat().st_mode & 0o777 == 0o700


def test_plugins_dir_under_meiva_home(unit_home: Path):
    assert paths.plugins_dir(home=unit_home) == unit_home / "plugins"


def test_plugins_dir_auto_mkdirs(unit_home: Path):
    target = unit_home / "plugins"
    assert not target.exists()
    paths.plugins_dir(home=unit_home)
    assert target.is_dir()


def test_workspace_paths_derive_from_the_workspaces_root(unit_home: Path):
    root = paths.workspaces_root(home=unit_home)
    assert root == unit_home / "workspaces"
    assert paths.workspaces_path(unit_home) == root
    assert paths.workspace_path(unit_home, 7) == root / "7"
    assert paths.workspace_dir(7, home=unit_home) == root / "7"


def test_default_home_is_bare_meiva():
    assert default_home() == Path("~/.meiva").expanduser()


def test_is_default_home_expands_and_compares():
    assert is_default_home(Path("~/.meiva"))
    assert not is_default_home(Path("~/.meiva-foo"))


def test_is_default_home_of_compares_against_the_given_account_not_the_process(tmp_path: Path):
    account = tmp_path / "account"
    assert is_default_home_of(account / ".meiva", user_home=account)
    assert is_default_home_of(account / "elsewhere" / ".." / ".meiva", user_home=account)
    assert not is_default_home_of(account / ".meiva-foo", user_home=account)
    assert not is_default_home_of(default_home(), user_home=account)


def test_is_default_home_of_follows_a_symlinked_spelling_of_the_same_directory(tmp_path: Path):
    account = tmp_path / "account"
    (account / ".meiva").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(account / ".meiva")

    assert is_default_home_of(alias, user_home=account)


def test_is_prod_install_needs_the_default_home_and_a_non_worktree_checkout(tmp_path: Path):
    home = default_home()
    assert is_prod_install(repo=tmp_path / "Meiva", home=home)
    assert not is_prod_install(repo=tmp_path / "Meiva" / ".worktrees" / "x", home=home)
    assert not is_prod_install(repo=tmp_path / ".claude" / "worktrees" / "x", home=home)
    assert not is_prod_install(repo=tmp_path / "Meiva", home=tmp_path / "scratch-home")


_RUNNER = frozenset({"agent-runner"})
_GATEWAY = frozenset({"gateway"})

_DERIVED = [
    (paths.plugins_dir, "plugins"),
    (paths.logs_dir, "logs"),
    (paths.permissions_helper_app_dir, "helper"),
    (paths.memory_dir, "memory"),
    (partial(paths.gateway_memory_dir, roles=_RUNNER), "gateway/memory"),
    (partial(paths.gateway_memory_dir, roles=_GATEWAY), "memory"),
    (paths.workspaces_root, "workspaces"),
    (partial(paths.workspace_dir, 7), "workspaces/7"),
    (paths.harness_sessions_dir, "harness/sessions"),
    (paths.harness_registrations_dir, "harness/registrations"),
]


# meiva:note
# Each helper derives its directory from the home it is handed.
@pytest.mark.parametrize(("derive", "relative"), _DERIVED)
def test_path_helpers_derive_from_an_explicit_home(
    derive: Callable[..., Path], relative: str, tmp_path_factory: pytest.TempPathFactory
):
    other = tmp_path_factory.mktemp("elsewhere") / "home"

    assert derive(home=other) == other / relative


# meiva:note
# The home is required: a helper called without one is a `TypeError`, not a read of the ambient
# home.
@pytest.mark.parametrize(("derive", "relative"), _DERIVED)
def test_a_helper_called_without_a_home_is_refused(
    derive: Callable[..., Path], relative: str, unit_home: Path
):
    with pytest.raises(TypeError, match="home"):
        derive()

    assert list(unit_home.iterdir()) == []


# meiva:note
# The home handed in is created owner-only like `meiva_home()` creates the ambient one, the helpers
# that create their directory still do, the pure workspace path does not, and the ambient home is
# not read: its mode stays what the test set, where `meiva_home()` would have repaired it to 0700.
def test_an_explicit_home_is_created_owner_only_and_the_ambient_home_is_not_read(
    unit_home: Path, tmp_path_factory: pytest.TempPathFactory
):
    other = tmp_path_factory.mktemp("elsewhere") / "home"
    unit_home.chmod(0o755)

    paths.workspace_dir(7, home=other)
    paths.plugins_dir(home=other)
    assert paths.workspace_path(other, 8) == other / "workspaces" / "8"

    assert other.stat().st_mode & 0o777 == 0o700
    assert (other / "plugins").is_dir()
    assert (other / "workspaces" / "7").is_dir()
    assert not (other / "workspaces" / "8").exists()
    assert unit_home.stat().st_mode & 0o777 == 0o755
    assert list(unit_home.iterdir()) == []


@pytest.mark.parametrize(("derive", "relative"), _DERIVED)
def test_a_relative_home_is_refused_before_anything_is_created(
    derive: Callable[..., Path],
    relative: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    refused = Path("never-created-relative-home-for-test")

    with pytest.raises(ValueError, match="absolute"):
        derive(home=refused)

    assert list(tmp_path.iterdir()) == []

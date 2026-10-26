"""base/paths/__init__.py unit tests — MEIVA_HOME resolution + path helpers + first-access mkdir."""

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from base import paths
from base.paths.home import default_home, is_default_home, is_prod_install


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
    assert paths.plugins_dir() == unit_home / "plugins"


def test_plugins_dir_auto_mkdirs(unit_home: Path):
    target = unit_home / "plugins"
    assert not target.exists()
    paths.plugins_dir()
    assert target.is_dir()


def test_workspace_paths_derive_from_the_workspaces_root(unit_home: Path):
    root = paths.workspaces_root()
    assert root == unit_home / "workspaces"
    assert paths.workspace_dir_readonly(7) == root / "7"
    assert paths.workspace_dir(7) == root / "7"


def test_default_home_is_bare_meiva():
    assert default_home() == Path("~/.meiva").expanduser()


def test_is_default_home_expands_and_compares():
    assert is_default_home(Path("~/.meiva"))
    assert not is_default_home(Path("~/.meiva-foo"))


def test_is_prod_install_needs_the_default_home_and_a_non_worktree_checkout(tmp_path: Path):
    home = default_home()
    assert is_prod_install(repo=tmp_path / "Meiva", home=home)
    assert not is_prod_install(repo=tmp_path / "Meiva" / ".worktrees" / "x", home=home)
    assert not is_prod_install(repo=tmp_path / ".claude" / "worktrees" / "x", home=home)
    assert not is_prod_install(repo=tmp_path / "Meiva", home=tmp_path / "scratch-home")


_RUNNER = frozenset({"agent-runner"})
_GATEWAY = frozenset({"gateway"})


# meiva:note
# Each helper derives its directory from the home it is handed.
@pytest.mark.parametrize(
    ("derive", "relative"),
    [
        (partial(paths.plugins_dir), "plugins"),
        (partial(paths.logs_dir), "logs"),
        (partial(paths.permissions_helper_app_dir), "helper"),
        (partial(paths.memory_dir), "memory"),
        (partial(paths.gateway_memory_dir, roles=_RUNNER), "gateway/memory"),
        (partial(paths.gateway_memory_dir, roles=_GATEWAY), "memory"),
        (partial(paths.workspaces_root), "workspaces"),
        (partial(paths.workspace_dir, 7), "workspaces/7"),
        (partial(paths.workspace_dir_readonly, 8), "workspaces/8"),
        (partial(paths.harness_sessions_dir), "harness/sessions"),
        (partial(paths.harness_registrations_dir), "harness/registrations"),
    ],
)
def test_path_helpers_derive_from_an_explicit_home(
    derive: Callable[..., Path], relative: str, tmp_path_factory: pytest.TempPathFactory
):
    other = tmp_path_factory.mktemp("elsewhere") / "home"

    assert derive(home=other) == other / relative


# meiva:note
# An explicit home is created owner-only like `meiva_home()` creates the ambient one, the helpers
# that create their directory still do, the read-only one does not, and the ambient home stays
# untouched.
def test_an_explicit_home_is_created_owner_only_and_the_ambient_home_is_left_alone(
    unit_home: Path, tmp_path_factory: pytest.TempPathFactory
):
    other = tmp_path_factory.mktemp("elsewhere") / "home"

    paths.workspace_dir(7, home=other)
    paths.plugins_dir(home=other)
    paths.workspace_dir_readonly(8, home=other)

    assert other.stat().st_mode & 0o777 == 0o700
    assert (other / "plugins").is_dir()
    assert (other / "workspaces" / "7").is_dir()
    assert not (other / "workspaces" / "8").exists()
    assert list(unit_home.iterdir()) == []

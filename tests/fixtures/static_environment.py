from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest

from meizel.checkout import lanes_at


class TestEnvironment(StrEnum):
    NATIVE = "native"
    STATIC = "static"


@dataclass(frozen=True, slots=True)
class _Ownership:
    raw_root: Path
    root: Path
    static_files: frozenset[str]
    native_files: frozenset[str]
    dirs_with_static: frozenset[str]
    pure_static_dirs: frozenset[str]


_OWNERSHIP_KEY = pytest.StashKey[_Ownership]()


def static_mode(config: pytest.Config) -> bool:
    return TestEnvironment(config.getoption("test_environment")) is TestEnvironment.STATIC


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("Meiva test environment")
    group.addoption(
        "--test-environment",
        choices=tuple(TestEnvironment),
        default=TestEnvironment.NATIVE,
        help="Use the default native data plane or the isolated static-contract process.",
    )
    group.addoption(
        "--omit-static-tests",
        action="store_true",
        help="Exclude the static lane already verified by required CI structure checks.",
    )


def _ancestor_dirs(paths: frozenset[str]) -> frozenset[str]:
    dirs: set[str] = set()
    for rel in paths:
        dirs.add("")
        curr = rel.rpartition("/")[0]
        while curr:
            dirs.add(curr)
            curr = curr.rpartition("/")[0]
    return frozenset(dirs)


def _ownership(config: pytest.Config) -> _Ownership:
    cached = config.stash.get(_OWNERSHIP_KEY, None)
    if cached is not None:
        return cached
    raw_root = config.rootpath.absolute()
    root = config.rootpath.resolve()
    lanes = lanes_at(root)
    dirs_static = _ancestor_dirs(lanes.static)
    dirs_native = _ancestor_dirs(lanes.native)
    built = _Ownership(
        raw_root=raw_root,
        root=root,
        static_files=lanes.static,
        native_files=lanes.native,
        dirs_with_static=dirs_static,
        pure_static_dirs=dirs_static - dirs_native,
    )
    config.stash[_OWNERSHIP_KEY] = built
    return built


def _relative_parts_from_root(idx: _Ownership, target: Path) -> tuple[str, ...] | None:
    abs_target = target.absolute()
    for candidate_root in (idx.raw_root, idx.root):
        try:
            return abs_target.relative_to(candidate_root).parts
        except ValueError:
            continue
    return None


def _has_symlink_component(idx: _Ownership, target: Path) -> bool:
    if target.is_symlink():
        return True
    parts = _relative_parts_from_root(idx, target)
    if parts is None:
        return True
    probe = idx.root
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            return True
    return False


def _resolved_rel(idx: _Ownership, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(idx.root)
    except ValueError:
        return None
    return "" if str(rel) == "." else rel.as_posix()


def _is_wholly_static(idx: _Ownership, path: Path) -> bool:
    if _has_symlink_component(idx, path):
        return False
    rel = _resolved_rel(idx, path)
    if rel is None:
        return False
    return rel in idx.static_files or rel in idx.pure_static_dirs


def _argument_path(config: pytest.Config, arg: str) -> Path:
    path = Path(arg.split("::", 1)[0])
    return path if path.is_absolute() else config.invocation_params.dir.resolve() / path


def _refuse_native_data_plane(patch: pytest.MonkeyPatch) -> None:
    from base.deploy.tests import containers
    from tests.fixtures.guards import refuse_native_data_plane, stub_everywhere

    refuse_native_data_plane(
        patch, reason="Static tests cannot use Postgres or Redis; use the native test environment."
    )

    def refuse_container(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Static tests cannot use Postgres or Redis; use the native test environment.")

    stub_everywhere(patch, containers, "postgres", refuse_container)
    stub_everywhere(patch, containers, "redis_server", refuse_container)


def _select_static_paths(config: pytest.Config, idx: _Ownership) -> None:
    if not idx.static_files:
        raise pytest.UsageError("Static test derivation found no static tests in this checkout.")
    if config.args_source is not pytest.Config.ArgsSource.ARGS:
        config.args[:] = [
            arg
            for arg in config.args
            if (rel := _resolved_rel(idx, _argument_path(config, arg))) is not None
            and (rel in idx.dirs_with_static or rel in idx.static_files)
        ]
        return
    outside = [
        arg for arg in config.args if not _is_wholly_static(idx, _argument_path(config, arg))
    ]
    if outside:
        raise pytest.UsageError(f"Static processes accept only owned static test paths: {outside}")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if not static_mode(config) and not config.getoption("omit_static_tests"):
        return
    if static_mode(config) and config.getoption("omit_static_tests"):
        raise pytest.UsageError("The static environment cannot omit its own tests.")
    idx = _ownership(config)
    if not static_mode(config):
        config.args[:] = [
            arg for arg in config.args if not _is_wholly_static(idx, _argument_path(config, arg))
        ]
        return
    _select_static_paths(config, idx)
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    _refuse_native_data_plane(patch)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if not static_mode(config) and not config.getoption("omit_static_tests"):
        return None
    idx = _ownership(config)
    if static_mode(config):
        if _has_symlink_component(idx, collection_path):
            return None
        rel = _resolved_rel(idx, collection_path)
        if rel is None:
            return None
        if collection_path.is_dir():
            return True if rel not in idx.dirs_with_static else None
        name = collection_path.name
        if (
            name != "conftest.py"
            and (name.startswith("test_") or name.endswith("_test.py"))
            and rel not in idx.static_files
        ):
            return True
        return None
    if _is_wholly_static(idx, collection_path):
        return True
    return None


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if static_mode(config):
        idx = _ownership(config)
        outside = [
            item.nodeid
            for item in items
            if _has_symlink_component(idx, item.path)
            or _resolved_rel(idx, item.path) not in idx.static_files
        ]
        if outside:
            raise pytest.UsageError(f"Non-static tests reached the static process: {outside}")

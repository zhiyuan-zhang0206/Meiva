"""`scripts/lint/async_no_sync_blocking.py` — sync calls in async bodies, and a
current repo-helper list (a retired helper's name must not linger)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.lint import async_no_sync_blocking as _lint


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A scratch source tree with one repo helper defined in `base/`."""
    _write(tmp_path, "base/ops.py", "def sync_op() -> None:\n    pass\n")
    (tmp_path / "gateway").mkdir()
    return tmp_path


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _run_main(root: Path, helpers: set[str] | None = None) -> int:
    return _lint.main(
        [],
        repo_root=root,
        definition_dirs=("base",),
        scan_dirs=("gateway",),
        repo_blocking_helpers={"sync_op"} if helpers is None else helpers,
    )


def _stale(root: Path, helpers: set[str]) -> list[str]:
    return _lint.stale_repo_helpers(
        repo_root=root,
        definition_dirs=("base",),
        repo_blocking_helpers=helpers,
    )


def test_real_repo_helpers_are_all_defined() -> None:
    assert _lint.stale_repo_helpers() == []


def test_a_retired_helper_name_is_stale(tree: Path) -> None:
    assert _stale(tree, {"sync_op", "cluster_rollout_op"}) == ["cluster_rollout_op"]


def test_a_stale_helper_fails_the_lint(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_main(tree, {"sync_op", "cluster_update_op"}) == 1
    assert "stale REPO_BLOCKING_HELPERS entry 'cluster_update_op'" in capsys.readouterr().out


def test_async_helper_definitions_count(tree: Path) -> None:
    _write(tree, "base/aio.py", "async def async_op[T](value: T) -> T:\n    return value\n")
    assert _stale(tree, {"sync_op", "async_op"}) == []


def test_unthreaded_repo_helper_in_async_body_is_flagged(tree: Path) -> None:
    _write(
        tree,
        "gateway/route.py",
        """
        async def handler() -> None:
            sync_op()
        """,
    )
    assert _run_main(tree) == 1


def test_threaded_repo_helper_is_clean(tree: Path) -> None:
    _write(
        tree,
        "gateway/route.py",
        """
        import asyncio

        async def handler() -> None:
            await asyncio.to_thread(sync_op)
        """,
    )
    assert _run_main(tree) == 0

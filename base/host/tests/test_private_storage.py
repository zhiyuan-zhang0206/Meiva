from __future__ import annotations

import os
import shutil
import socket
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

import loguru
import pytest

from base.host import private_storage
from base.log import logger


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _format_summary(kwargs: dict[str, object]) -> str:
    return (
        f"private storage convergence skipped {kwargs['n']} node(s) under {kwargs['path']} "
        f"(symlink={kwargs['symlinks']}, foreign_owned={kwargs['foreign_owned']}, "
        f"non_regular={kwargs['non_regular']}; node_modules dirs not descended={kwargs['node_modules']}) "
        f"— first: {kwargs['examples']}"
    )


class _Captured:
    def __init__(self, cleanup: Callable[[Callable[[], object]], None]) -> None:
        self.debugs: list[tuple[str, dict[str, object]]] = []
        self.warnings: list[tuple[str, dict[str, object]]] = []
        sink_id = logger.add(self._sink, level="DEBUG")
        cleanup(lambda: logger.remove(sink_id))

    def _sink(self, message: loguru.Message) -> None:
        record = message.record
        extra: dict[str, object] = {
            str(k): v for k, v in record["extra"].items() if k != "agent_id"
        }
        level = record["level"].name
        text = record["message"]
        if level == "DEBUG":
            self.debugs.append((text, extra))
        elif level == "WARNING":
            self.warnings.append((text, extra))


def test_converge_skips_foreign_nodes(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    target = tmp_path / "tree"
    ours_dir = target / "ours"
    ours_dir.mkdir(parents=True)
    ours_dir.chmod(0o755)
    cap = _Captured(request.addfinalizer)
    skips = private_storage.SkipReport()
    private_storage.converge_child(ours_dir, skips)

    foreign_file = ours_dir / "secret"
    foreign_file.write_bytes(b"secret")
    foreign_file.chmod(0o400)
    foreign_dir = target / "foreign"
    foreign_dir.mkdir()
    foreign_dir.chmod(0o500)
    foreign_euid = os.geteuid() + 1
    private_storage.converge_child(foreign_file, skips, euid=foreign_euid)
    private_storage.converge_child(foreign_dir, skips, euid=foreign_euid)
    private_storage.report_skips(target, skips)

    assert _mode(foreign_file) == 0o400
    assert _mode(foreign_dir) == 0o500
    assert _mode(ours_dir) == 0o700
    assert {(message, str(kwargs["path"])) for message, kwargs in cap.debugs} == {
        (
            f"private storage convergence skipped foreign-owned path {foreign_file}",
            str(foreign_file),
        ),
        (f"private storage convergence skipped foreign-owned path {foreign_dir}", str(foreign_dir)),
    }
    assert len(cap.warnings) == 1
    message, kwargs = cap.warnings[0]
    assert message == _format_summary(kwargs)
    assert kwargs["n"] == 2
    assert kwargs["foreign_owned"] == 2
    assert kwargs["symlinks"] == 0
    assert kwargs["non_regular"] == 0
    assert kwargs["node_modules"] == 0
    examples = str(kwargs["examples"])
    assert str(foreign_file) in examples and str(foreign_dir) in examples


def test_private_file_repairs_mode_drift(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"secret")
    target.chmod(0o644)

    private_storage.ensure_private_file(target)

    assert target.read_bytes() == b"secret"
    assert _mode(target) == 0o600


def test_private_file_preserves_owner_execute_permission(tmp_path: Path) -> None:
    target = tmp_path / "hook"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o744)

    private_storage.ensure_private_file(target)

    assert _mode(target) == 0o700


def test_private_tree_recursively_repairs_existing_mode_drift(tmp_path: Path) -> None:
    root = tmp_path / "private"
    nested = root / "agent" / "artifacts"
    nested.mkdir(parents=True)
    payload = nested / "result.txt"
    payload.write_text("secret")
    for directory in (root, root / "agent", nested):
        directory.chmod(0o755)
    payload.chmod(0o644)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(root) == 0o700
    assert _mode(root / "agent") == 0o700
    assert _mode(nested) == 0o700
    assert _mode(payload) == 0o600


def test_private_tree_skips_nested_symlink(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o755)
    link = root / "link"
    link.symlink_to(target, target_is_directory=True)
    cap = _Captured(request.addfinalizer)

    assert private_storage.converge_private_tree(root) == root

    assert link.is_symlink()
    assert _mode(target) == 0o755
    assert cap.debugs == [
        (f"private storage convergence skipped symlink {link}", {"path": link}),
    ]
    expected_kwargs: dict[str, object] = {
        "n": 1,
        "path": root,
        "symlinks": 1,
        "foreign_owned": 0,
        "non_regular": 0,
        "node_modules": 0,
        "examples": f"{link} (symlink)",
    }
    assert cap.warnings == [
        (
            _format_summary(expected_kwargs),
            expected_kwargs,
        )
    ]


def test_private_write_replaces_existing_content_without_permissive_intermediate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"old")
    target.chmod(0o644)

    private_storage.write_private_bytes(target, b"new")

    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []


def test_private_write_cleans_up_temp_file_when_replace_fails(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "secret"
    existing.write_bytes(b"old-complete")
    with pytest.raises(FileExistsError):
        private_storage.create_private_bytes(existing, b"new-complete")
    assert existing.read_bytes() == b"old-complete"
    assert list(tmp_path.glob(".*.tmp")) == []

    directory_target = tmp_path / "dir-target"
    directory_target.mkdir()
    with pytest.raises(IsADirectoryError):
        private_storage.write_private_bytes(directory_target, b"new-complete")
    assert directory_target.is_dir()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_converge_skips_foreign_owned_root(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    root.chmod(0o755)
    child = root / "secret"
    child.write_bytes(b"secret")
    child.chmod(0o644)
    cap = _Captured(request.addfinalizer)

    assert private_storage.converge_private_tree(root, euid=os.geteuid() + 1) == root

    assert _mode(root) == 0o755
    assert _mode(child) == 0o644
    assert cap.warnings == [
        (f"private storage convergence skipped foreign-owned path {root}", {"path": root})
    ]
    assert cap.debugs == []


def test_private_tree_skips_live_unix_socket(request: pytest.FixtureRequest) -> None:
    root = Path(tempfile.mkdtemp(prefix="meiva-converge-", dir="/tmp"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        root.chmod(0o755)
        payload = root / "result.txt"
        payload.write_text("secret")
        payload.chmod(0o644)
        socket_path = root / "app.sock"
        server.bind(str(socket_path))
        cap = _Captured(request.addfinalizer)

        assert private_storage.converge_private_tree(root) == root

        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        assert _mode(root) == 0o700
        assert _mode(payload) == 0o600
        assert cap.debugs == [
            (
                f"private storage convergence skipped non-regular file {socket_path}",
                {"path": socket_path},
            ),
        ]
        expected_kwargs: dict[str, object] = {
            "n": 1,
            "path": root,
            "symlinks": 0,
            "foreign_owned": 0,
            "non_regular": 1,
            "node_modules": 0,
            "examples": f"{socket_path} (non-regular)",
        }
        assert cap.warnings == [
            (
                _format_summary(expected_kwargs),
                expected_kwargs,
            )
        ]
    finally:
        server.close()
        shutil.rmtree(root)


def test_private_tree_skips_fifo(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    fifo = root / "queue"
    os.mkfifo(fifo)
    cap = _Captured(request.addfinalizer)

    assert private_storage.converge_private_tree(root) == root

    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert _mode(root) == 0o700
    assert cap.debugs == [
        (f"private storage convergence skipped non-regular file {fifo}", {"path": fifo}),
    ]
    expected_kwargs: dict[str, object] = {
        "n": 1,
        "path": root,
        "symlinks": 0,
        "foreign_owned": 0,
        "non_regular": 1,
        "node_modules": 0,
        "examples": f"{fifo} (non-regular)",
    }
    assert cap.warnings == [
        (
            _format_summary(expected_kwargs),
            expected_kwargs,
        )
    ]


def test_converge_does_not_descend_node_modules(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    root = tmp_path / "private"
    modules = root / "pkg" / "node_modules"
    inner = modules / "dep"
    inner.mkdir(parents=True)
    payload = inner / "index.js"
    payload.write_bytes(b"module.exports = {};\n")
    payload.chmod(0o644)
    (inner / "linked").symlink_to(inner / "index.js")
    fifo = inner / "queue"
    os.mkfifo(fifo)
    top = root / "keep.txt"
    top.write_bytes(b"secret")
    top.chmod(0o644)
    cap = _Captured(request.addfinalizer)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(modules) == 0o700
    assert _mode(payload) == 0o644
    assert _mode(top) == 0o600
    assert cap.debugs == []
    assert cap.warnings == []
    assert private_storage.scan_non_regular_nodes(root) == []


def test_summary_caps_examples_and_counts_every_skip(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    root = tmp_path / "private"
    root.mkdir()
    links: list[Path] = []
    for index in range(5):
        link = root / f"link{index}"
        link.symlink_to(tmp_path / "missing-target")
        links.append(link)
    cap = _Captured(request.addfinalizer)

    assert private_storage.converge_private_tree(root) == root

    assert len(cap.warnings) == 1
    message, kwargs = cap.warnings[0]
    assert message == _format_summary(kwargs)
    assert kwargs["n"] == 5
    assert kwargs["symlinks"] == 5
    examples = str(kwargs["examples"])
    assert examples.endswith("…")
    assert sum(str(link) in examples for link in links) == 3


def test_scan_non_regular_nodes_matches_what_converge_skips(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    root = tmp_path / "private"
    nested = root / "agent"
    nested.mkdir(parents=True)
    (nested / "result.txt").write_text("secret")
    fifo = nested / "queue"
    os.mkfifo(fifo)
    target = tmp_path / "outside"
    target.mkdir()
    (root / "link").symlink_to(target, target_is_directory=True)

    assert private_storage.scan_non_regular_nodes(root) == [fifo]

    cap = _Captured(request.addfinalizer)
    assert private_storage.converge_private_tree(root) == root
    assert (
        f"private storage convergence skipped non-regular file {fifo}",
        {"path": fifo},
    ) in cap.debugs
    assert len(cap.warnings) == 1
    assert cap.warnings[0][1]["symlinks"] == 1
    assert cap.warnings[0][1]["non_regular"] == 1
    assert cap.warnings[0][1]["n"] == 2


def test_scan_non_regular_nodes_missing_root_is_empty(tmp_path: Path) -> None:
    assert private_storage.scan_non_regular_nodes(tmp_path / "absent") == []


def test_scan_non_regular_nodes_does_not_follow_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    os.mkfifo(target / "queue")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    assert private_storage.scan_non_regular_nodes(link) == []


def test_private_tree_root_problem_matches_converges_refusals(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert private_storage.private_tree_root_problem(root) is None

    root.write_text("file")
    assert private_storage.private_tree_root_problem(root) == "is not a directory"

    root.unlink()
    target = tmp_path / "target"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    assert private_storage.private_tree_root_problem(root) == "is a symlink"


def test_private_file_problem_matches_the_writer_refusal(tmp_path: Path) -> None:
    path = tmp_path / "marker"
    assert private_storage.private_file_problem(path) is None

    path.symlink_to(tmp_path / "elsewhere")
    assert private_storage.private_file_problem(path) == "is a symlink"


def test_private_file_problem_sees_a_fifo(tmp_path: Path) -> None:
    path = tmp_path / "marker"
    os.mkfifo(path)
    assert private_storage.private_file_problem(path) == "is not a regular file"

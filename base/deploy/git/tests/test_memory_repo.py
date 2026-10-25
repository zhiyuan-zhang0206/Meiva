from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.deploy.git import memory_repo
from base.deploy.git.tests.memory_checkouts import commit_note
from base.deploy.git.tests.memory_checkouts import memory_checkout as memory_checkout
from base.paths import gateway_memory_dir
from base.paths.tests.homes import default_home as default_home


def test_branch_name_gateway_tracks_main(*, machine_identity: MachineIdentity) -> None:
    machine_identity.set(role="gateway", name="cloud")
    assert memory_repo.branch_name(machine=machine_identity) == "main"


def test_branch_name_agent_runner_authors_machine_branch(
    *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role="agent-runner", name="test-host-2")
    assert memory_repo.branch_name(machine=machine_identity) == "machine-test-host-2"


def test_branch_name_combined_unit_returns_machine_branch(
    *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role="gateway,agent-runner", name="test-host")
    assert memory_repo.branch_name(machine=machine_identity) == "machine-test-host"


def test_pull_main_git_sequence_and_returned_sha(memory_checkout: tuple[Path, Path]) -> None:
    source, root = memory_checkout
    memory_repo.run_git("checkout", "-b", "consolidated", cwd=source)
    expected = commit_note(source, "consolidated update")
    memory_repo.run_git("push", "origin", "consolidated", cwd=source)

    sha = memory_repo.pull_main(root=root, keep_local=False, branch="consolidated")

    assert sha == expected
    assert memory_repo.run_git("rev-parse", "HEAD", cwd=root) == expected
    assert "consolidated update" in (root / "note.md").read_text()


def test_pull_main_rejects_divergence(memory_checkout: tuple[Path, Path]) -> None:
    source, root = memory_checkout
    commit_note(source, "remote change")
    memory_repo.run_git("push", "origin", "main", cwd=source)
    local = commit_note(root, "unpublished local change")

    with pytest.raises(subprocess.CalledProcessError):
        memory_repo.pull_main(root=root, keep_local=False, branch="main")

    assert memory_repo.run_git("rev-parse", "HEAD", cwd=root) == local
    assert "unpublished local change" in (root / "note.md").read_text()


def test_gateway_memory_dir_combined_uses_subdirectory(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    from base.paths import meiva_home

    machine_identity.set(role="gateway,agent-runner", name="test-host")
    gmd = gateway_memory_dir(roles=machine_identity.role())
    home = meiva_home()
    assert gmd == home / "gateway" / "memory"

    from base.paths import memory_dir

    assert gmd != memory_dir()


def test_gateway_memory_dir_gateway_only_equals_memory_dir(
    *, machine_identity: MachineIdentity
) -> None:
    from base.paths import meiva_home, memory_dir

    machine_identity.set(role="gateway", name="cloud")
    gmd = gateway_memory_dir(roles=machine_identity.role())
    assert gmd == memory_dir()
    assert gmd == meiva_home() / "memory"


def test_pull_main_keep_local_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "local"
    memory_repo.init_local_repo("main", root)
    expected = commit_note(root, "local memory")

    assert memory_repo.run_git("remote", cwd=root) == ""
    assert memory_repo.pull_main(root=root, keep_local=True, branch="absent") == expected
    assert (root / "note.md").read_text().endswith("local memory\n")


@pytest.mark.parametrize("branch", ["main", "machine-runner"])
def test_reconcile_correct_branch_preserves_dirty_files(tmp_path: Path, branch: str) -> None:
    memory_repo.init_local_repo(branch, tmp_path)
    note = tmp_path / "note.md"
    note.write_text("unpublished")
    before = memory_repo.run_git("rev-parse", "HEAD", cwd=tmp_path)
    memory_repo.reconcile_initialized(
        root=tmp_path, branch=branch, keep_local=False, allow_main_switch=False
    )
    assert memory_repo.run_git("rev-parse", "HEAD", cwd=tmp_path) == before
    assert note.read_text() == "unpublished"


@pytest.mark.parametrize("keep_local", [False, True])
def test_reconcile_gateway_can_leave_stale_branch(
    memory_checkout: tuple[Path, Path], keep_local: bool
) -> None:
    _, root = memory_checkout
    memory_repo.run_git("checkout", "-b", "machine-cloud", cwd=root)
    memory_repo.reconcile_initialized(
        root=root, branch="main", keep_local=keep_local, allow_main_switch=True
    )
    assert memory_repo.run_git("branch", "--show-current", cwd=root) == "main"
    assert memory_repo.run_git("remote", cwd=root) == ("" if keep_local else "origin")


@pytest.mark.parametrize("branch", ["machine-runner", "machine-combined"])
def test_reconcile_runner_mismatch_does_not_discard_work(
    memory_checkout: tuple[Path, Path], branch: str
) -> None:
    _, root = memory_checkout
    local = commit_note(root, "unpublished local work")
    with pytest.raises(memory_repo.MemoryBranchMismatch, match="Manually switch"):
        memory_repo.reconcile_initialized(
            root=root, branch=branch, keep_local=False, allow_main_switch=False
        )
    assert memory_repo.run_git("branch", "--show-current", cwd=root) == "main"
    assert memory_repo.run_git("rev-parse", "HEAD", cwd=root) == local


def test_reconcile_keep_local_removes_every_remote(memory_checkout: tuple[Path, Path]) -> None:
    _, root = memory_checkout
    memory_repo.run_git("remote", "add", "backup", "/unused", cwd=root)
    memory_repo.reconcile_initialized(
        root=root, branch="main", keep_local=True, allow_main_switch=False
    )
    assert memory_repo.run_git("remote", cwd=root) == ""


@pytest.mark.parametrize("branch", ["main", "machine-new"])
def test_init_clones_selected_local_remote_and_publishes_branch(
    memory_checkout: tuple[Path, Path], tmp_path: Path, branch: str
) -> None:
    source, existing = memory_checkout
    remote = memory_repo.run_git("remote", "get-url", "origin", cwd=source)
    root = tmp_path / "selected"
    memory_repo.init(root=root, branch=branch, remote=remote)
    assert memory_repo.is_initialized(root=root)
    assert memory_repo.run_git("branch", "--show-current", cwd=root) == branch
    assert memory_repo.run_git("rev-parse", "HEAD", cwd=root) == memory_repo.run_git(
        "rev-parse", "HEAD", cwd=existing
    )
    assert memory_repo.run_git("ls-remote", "--heads", "origin", branch, cwd=root)
    assert (root / ".gitignore").read_text() == (source / ".gitignore").read_text()


def test_init_seeds_gitignore_when_remote_has_none(tmp_path: Path, default_home: Path) -> None:
    default_home.mkdir(parents=True, exist_ok=True)
    (default_home / ".gitconfig").write_text("[user]\n  name = Test\n  email = test@local\n")
    source = tmp_path / "source"
    memory_repo.init_local_repo("main", source)
    memory_repo.run_git("rm", ".gitignore", cwd=source)
    memory_repo.run_git("commit", "-qm", "remove ignore", cwd=source)
    remote = tmp_path / "bare.git"
    memory_repo.run_git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    root = tmp_path / "checkout"
    memory_repo.init(root=root, branch="main", remote=str(remote))
    assert (root / ".gitignore").is_file()
    assert memory_repo.run_git("log", "-1", "--format=%s", cwd=root) == "init: .gitignore on main"


def test_memory_remote_requires_explicit_nonempty_value() -> None:
    assert memory_repo.memory_remote("  /local/remote.git ") == "/local/remote.git"
    with pytest.raises(memory_repo.MemoryRemoteMissing):
        memory_repo.memory_remote("   ")


def test_run_git_is_bounded_and_kills_the_tree_on_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[dict[str, object]] = []

    def _fake_run_bounded(
        argv: Sequence[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append({"argv": list(argv), "timeout": timeout})
        return subprocess.CompletedProcess(list(argv), 0, "abc123\n", "")

    monkeypatch.setattr(memory_repo, "run_bounded", _fake_run_bounded)

    assert memory_repo.run_git("rev-parse", "HEAD", cwd=tmp_path) == "abc123"
    assert seen[0]["argv"] == ["git", "rev-parse", "HEAD"]
    assert seen[0]["timeout"] == memory_repo.GIT_TIMEOUT_S
    assert isinstance(seen[0]["timeout"], float)


def test_run_git_raises_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:

    def _fails(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, "", "fatal: no remote")

    monkeypatch.setattr(memory_repo, "run_bounded", _fails)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        memory_repo.run_git("fetch", "origin", "main", cwd=tmp_path)
    assert exc.value.returncode == 1
    assert exc.value.stderr == "fatal: no remote"


def test_run_git_propagates_the_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:

    def _expire(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), memory_repo.GIT_TIMEOUT_S)

    monkeypatch.setattr(memory_repo, "run_bounded", _expire)
    with pytest.raises(subprocess.TimeoutExpired):
        memory_repo.run_git("fetch", "origin", "main", cwd=tmp_path)

# meiva:note
# Explicit checkout status and real HTTP/Git pool bootstrap contracts.
# meiva:link base/deploy/git/tests/docs/memory-repo-tests.meiva.okf.md

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from base.cluster.machine import GatewayAccess, GatewayApiTokenMissing
from base.deploy.git import memory_repo
from base.deploy.git.tests.memory_checkouts import commit_note, make_bundle
from base.deploy.git.tests.memory_checkouts import memory_checkout as memory_checkout
from base.deploy.git.tests.pool_server import PoolResponse, pool_server


def test_status_last_fetch_renders_cluster_zone(memory_checkout: tuple[Path, Path]) -> None:
    _, root = memory_checkout
    fetch_head = root / ".git" / "FETCH_HEAD"
    fetch_head.write_text("dummy\n")
    fixed = dt.datetime(2026, 1, 3, 9, 5, tzinfo=dt.UTC).timestamp()
    os.utime(fetch_head, (fixed, fixed))
    status = memory_repo.status(root=root, track_branch="main", zone=ZoneInfo("Asia/Shanghai"))
    assert status.last_fetch == "2026-01-03T17:05:00+08:00"
    assert status.branch == "main"
    assert status.is_clean
    assert status.local_commits_on_branch == 2
    assert status.ahead_of_main == status.behind_main == 0


def test_status_missing_tracking_reference_and_fetch_head(tmp_path: Path) -> None:
    memory_repo.init_local_repo("machine-runner", tmp_path)
    (tmp_path / "untracked.md").write_text("dirty")
    status = memory_repo.status(root=tmp_path, track_branch="missing", zone=dt.UTC)
    assert status.branch == "machine-runner"
    assert not status.is_clean
    assert status.local_commits_on_branch == 1
    assert status.ahead_of_main == status.behind_main == 0
    assert status.last_fetch == "never"


def test_status_uninitialized_does_not_create_checkout(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises(memory_repo.MemoryRepoUninitialized, match=str(root)):
        memory_repo.status(root=root, track_branch="main", zone=dt.UTC)
    assert not root.exists()


_POOL_CREDENTIAL = "pool-test-only"


def access(url: str) -> GatewayAccess:
    return GatewayAccess(
        gateway_url=url,
        cluster_secret=_POOL_CREDENTIAL,
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=True,
    )


def test_bootstrap_from_gateway_clones_bundle_as_machine_branch(tmp_path: Path) -> None:
    head, bundle = make_bundle(tmp_path / "src")
    target = tmp_path / "selected" / "memory"
    target.mkdir(parents=True)
    (target / "pre-init-leftover").write_text("discard")
    response = PoolResponse(bundle, head=head)
    with pool_server(response) as url:
        memory_repo.bootstrap_from_gateway(
            target=target, branch="machine-test-host-2", access=access(url)
        )
    assert response.path == "/api/memory/pool"
    assert response.headers["Authorization"] == f"Bearer {_POOL_CREDENTIAL}"
    assert (target / "MEMORY.md").read_text() == "# shared index\n"
    assert (target / "health" / "a.md").read_text() == "note"
    assert (target / ".git").is_dir()
    assert not (target / "pre-init-leftover").exists()
    assert memory_repo.run_git("branch", "--show-current", cwd=target) == "machine-test-host-2"
    assert memory_repo.run_git("remote", cwd=target) == ""
    assert memory_repo.run_git("rev-parse", "HEAD", cwd=target) == head
    assert not list(target.parent.glob(".memory-bootstrap-*"))


def test_bootstrap_from_gateway_head_mismatch_fails_loud(tmp_path: Path) -> None:
    _, bundle = make_bundle(tmp_path / "src")
    target = tmp_path / "selected" / "memory"
    target.mkdir(parents=True)
    leftover = target / "pre-init-leftover"
    leftover.write_text("retain until a valid checkout is ready")
    with (
        pool_server(PoolResponse(bundle, head="deadbeefdeadbeef")) as url,
        pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="does not match"),
    ):
        memory_repo.bootstrap_from_gateway(
            target=target, branch="machine-runner", access=access(url)
        )
    assert leftover.read_text() == "retain until a valid checkout is ready"
    assert not (target / ".git").exists()
    assert not list(target.parent.glob(".memory-bootstrap-*"))


def test_bootstrap_from_gateway_non_200_fails_loud(tmp_path: Path) -> None:
    target = tmp_path / "memory"
    with (
        pool_server(PoolResponse(b"unavailable", status=502)) as url,
        pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="HTTP 502"),
    ):
        memory_repo.bootstrap_from_gateway(
            target=target, branch="machine-runner", access=access(url)
        )
    assert not target.exists()


def test_bootstrap_from_gateway_missing_gateway_url_fails_loud(tmp_path: Path) -> None:
    target = tmp_path / "memory"
    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="gateway URL unavailable"):
        memory_repo.bootstrap_from_gateway(
            target=target, branch="machine-runner", access=access("")
        )
    assert not target.exists()


def test_bootstrap_from_gateway_corrupt_bundle_fails_loud(tmp_path: Path) -> None:
    target = tmp_path / "selected" / "memory"
    with (
        pool_server(PoolResponse(b"this is not a git bundle")) as url,
        pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="git clone from bundle failed"),
    ):
        memory_repo.bootstrap_from_gateway(
            target=target, branch="machine-runner", access=access(url)
        )
    assert not target.exists()
    assert not list(target.parent.glob(".memory-bootstrap-*"))


def test_status_compares_the_supplied_tracking_branch(memory_checkout: tuple[Path, Path]) -> None:
    source, root = memory_checkout
    memory_repo.run_git("checkout", "-b", "tracked", cwd=source)
    commit_note(source, "remote tracked change")
    memory_repo.run_git("push", "origin", "tracked", cwd=source)
    commit_note(root, "local unpublished change")
    memory_repo.run_git("fetch", "origin", "tracked", cwd=root)
    status = memory_repo.status(root=root, track_branch="tracked", zone=dt.UTC)
    assert status.branch == "main"
    assert status.ahead_of_main == status.behind_main == 1
    assert status.is_clean
    assert status.local_commits_on_branch == 3


def test_bootstrap_missing_credential_is_not_reclassified(tmp_path: Path) -> None:
    no_credential = GatewayAccess(
        gateway_url="http://127.0.0.1:1",
        cluster_secret="",
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=True,
    )
    with pytest.raises(GatewayApiTokenMissing):
        memory_repo.bootstrap_from_gateway(
            target=tmp_path / "memory", branch="machine-runner", access=no_credential
        )
    assert not (tmp_path / "memory").exists()

from __future__ import annotations

import contextlib
import datetime
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from base.cluster.machine import GatewayAccess, MachineIdentityReader
from base.host.proc import run_bounded

_DEFAULT_GITIGNORE = """\
# Meiva memory pool — markdown notes only; swap / cache files do not enter history.
.DS_Store
*.swp
*.swo
*~
.cache/
"""


class MemoryRemoteMissing(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


class MemoryPoolBootstrapFailed(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


class MemoryRepoUninitialized(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


class MemoryBranchMismatch(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


def memory_remote(raw: str) -> str:
    env = raw.strip()
    if env:
        return env
    raise MemoryRemoteMissing(
        "memory remote not set. Configure one before a remote-backed memory "
        "operation: set MEIVA_MEMORY_REMOTE=<git-url> in the home's `.env`."
    )


def branch_name(*, machine: MachineIdentityReader) -> str:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if "agent-runner" in machine.role():
        return f"machine-{machine.name()}"
    return "main"


def pull_main(*, root: Path, keep_local: bool, branch: str) -> str:
    # meiva:note
    # pull_main fast-forwards the supplied root from origin/<branch> and returns its HEAD. keep_local only reads HEAD, including when no remote or requested branch exists. Divergence fails without discarding local commits.
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if keep_local:
        return run_git("rev-parse", "HEAD", cwd=root)
    run_git("fetch", "origin", branch, cwd=root)
    run_git("merge", "--ff-only", f"origin/{branch}", cwd=root)
    return run_git("rev-parse", "HEAD", cwd=root)


def is_initialized(*, root: Path) -> bool:
    return (root / ".git").is_dir()


GIT_TIMEOUT_S = 120.0


def run_git(*args: str, cwd: Path) -> str:
    # meiva:note
    # run_git passes the per-command timeout to run_bounded, raises CalledProcessError for nonzero exits, and propagates TimeoutExpired. It returns stripped stdout only on success.
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    argv = ["git", *args]

    result = run_bounded(
        argv,
        timeout=GIT_TIMEOUT_S,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, argv, output=result.stdout, stderr=result.stderr
        )
    return result.stdout.strip()


def init_local_repo(branch: str, cwd: Path) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    cwd.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "init", "-q", "-b", branch, str(cwd)], check=True
    )
    run_git("config", "user.email", "meiva@local", cwd=cwd)
    run_git("config", "user.name", "Meiva Local", cwd=cwd)
    gitignore = cwd / ".gitignore"
    gitignore.write_text(_DEFAULT_GITIGNORE)
    run_git("add", ".gitignore", cwd=cwd)
    run_git("commit", "-q", "-m", f"init: local memory on {branch}", cwd=cwd)


def _strip_remotes(cwd: Path) -> None:
    for name in run_git("remote", cwd=cwd).split():
        run_git("remote", "remove", name, cwd=cwd)


POOL_SNAPSHOT_MAX_BYTES = 256 * 1024 * 1024
_POOL_FETCH_TIMEOUT_S = 120.0


def _stream_pool(client: Any, url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code != 200:
            raise MemoryPoolBootstrapFailed(f"GET {url} -> HTTP {resp.status_code}")
        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > POOL_SNAPSHOT_MAX_BYTES:
            raise MemoryPoolBootstrapFailed(
                f"pool snapshot declared at {declared} bytes, over the {POOL_SNAPSHOT_MAX_BYTES}-byte cap"
            )
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > POOL_SNAPSHOT_MAX_BYTES:
                raise MemoryPoolBootstrapFailed(
                    f"pool snapshot exceeds the {POOL_SNAPSHOT_MAX_BYTES}-byte cap"
                )
        head = resp.headers.get("X-Pool-Head")
        return head, bytes(buf)


def download_pool_snapshot(url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    import httpx

    try:
        with httpx.Client(timeout=_POOL_FETCH_TIMEOUT_S) as client:
            return _stream_pool(client, url, headers)
    except httpx.HTTPError as e:
        raise MemoryPoolBootstrapFailed(f"GET {url} failed: {e}") from e


def bootstrap_from_gateway(*, target: Path, branch: str, access: GatewayAccess) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    import shutil
    import tempfile

    from base.cluster.machine import gateway_api_base, gateway_auth_headers

    try:
        base = gateway_api_base(access.gateway_url)
    except Exception as e:
        raise MemoryPoolBootstrapFailed(f"gateway URL unavailable: {e}") from e
    url = f"{base}/api/memory/pool"
    head, bundle_bytes = download_pool_snapshot(url, gateway_auth_headers(access))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".memory-bootstrap-", dir=str(target.parent)))
    try:
        checkout_dir = _unpack_bundle(bundle_bytes, head, tmp_dir, branch=branch)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        checkout_dir.replace(target)
    except MemoryPoolBootstrapFailed:
        raise
    except (subprocess.CalledProcessError, OSError) as e:
        raise MemoryPoolBootstrapFailed(f"pool bootstrap failed: {e}") from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _unpack_bundle(bundle_bytes: bytes, head: str | None, tmp_dir: Path, *, branch: str) -> Path:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    bundle_path = tmp_dir / "pool.bundle"
    bundle_path.write_bytes(bundle_bytes)
    checkout_dir = tmp_dir / "pool"
    result = run_bounded(
        ["git", "clone", "-q", str(bundle_path), str(checkout_dir)],
        timeout=GIT_TIMEOUT_S,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MemoryPoolBootstrapFailed(
            f"git clone from bundle failed: {(result.stderr or '').strip()[:200]}"
        )
    if head:
        actual = run_git("rev-parse", "HEAD", cwd=checkout_dir)
        if actual != head:
            raise MemoryPoolBootstrapFailed(
                f"bundle HEAD {actual} does not match the advertised pool HEAD {head}"
            )

    run_git("remote", "remove", "origin", cwd=checkout_dir)
    run_git("checkout", "-q", "-b", branch, cwd=checkout_dir)
    return checkout_dir


@dataclass(frozen=True)
class RepoStatus:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    branch: str
    is_clean: bool
    local_commits_on_branch: int
    ahead_of_main: int
    behind_main: int
    last_fetch: str


def reconcile_initialized(
    *, root: Path, branch: str, keep_local: bool, allow_main_switch: bool
) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    current = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if current != branch:
        if allow_main_switch:
            if not keep_local:
                run_git("fetch", "origin", "main", cwd=root)
            run_git("checkout", "main", cwd=root)
            if keep_local:
                _strip_remotes(root)
            return
        raise MemoryBranchMismatch(
            f"{root} is already a git repo but on branch {current!r}, expected {branch!r}. "
            f"Manually switch: `git -C {root} checkout {branch}` "
            "(confirm working tree has no unpushed in-flight work)."
        )
    if keep_local:
        _strip_remotes(root)


def init(*, root: Path, branch: str, remote: str) -> None:
    # meiva:note
    # Clone the selected remote into the selected checkout, select or publish the requested branch, and seed the shared gitignore only when missing. The provisioning root decides whether a checkout needs initialization before calling this operation.
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    subprocess.run(  # noqa: S603
        ["git", "clone", remote, str(root)],
        check=True,
    )

    remote_branches = run_git("ls-remote", "--heads", "origin", branch, cwd=root)
    if remote_branches:
        run_git("checkout", branch, cwd=root)
    else:
        run_git("checkout", "-b", branch, cwd=root)
        run_git("push", "-u", "origin", branch, cwd=root)

    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_DEFAULT_GITIGNORE)
        run_git("add", ".gitignore", cwd=root)
        run_git("commit", "-m", f"init: .gitignore on {branch}", cwd=root)
        run_git("push", cwd=root)


def status(*, root: Path, track_branch: str, zone: datetime.tzinfo) -> RepoStatus:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if not is_initialized(root=root):
        raise MemoryRepoUninitialized(
            f"{root} is not yet a git repo — run `meiva memory init` first."
        )

    branch = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    is_clean = run_git("status", "--porcelain", cwd=root) == ""
    local_commits = int(run_git("rev-list", "--count", "HEAD", cwd=root))

    ahead = behind = 0
    with contextlib.suppress(subprocess.CalledProcessError):
        counts = run_git(
            "rev-list", "--left-right", "--count", f"HEAD...origin/{track_branch}", cwd=root
        )
        ahead_s, behind_s = counts.split()
        ahead, behind = int(ahead_s), int(behind_s)

    fetch_head = root / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        last_fetch = (
            datetime.datetime.fromtimestamp(
                fetch_head.stat().st_mtime,
                tz=datetime.UTC,
            )
            .astimezone(zone)
            .isoformat(timespec="seconds")
        )
    else:
        last_fetch = "never"

    return RepoStatus(
        branch=branch,
        is_clean=is_clean,
        local_commits_on_branch=local_commits,
        ahead_of_main=ahead,
        behind_main=behind,
        last_fetch=last_fetch,
    )

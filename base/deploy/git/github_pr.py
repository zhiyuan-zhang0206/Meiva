"""Detect whether this host can open and merge GitHub PRs on the memory repo.

The memory pool is consolidated nightly by agents that run `gh pr create` /
`gh pr merge` and `git push` on each machine's memory repo: a per-machine
steward stages + pushes its `machine-<name>` branch and opens a PR, and an
arbiter merges the night's PRs into `main`. A machine missing `gh`, not
authenticated, or lacking write permission silently breaks that nightly sync.
The host-convergence check uses this to fail fast when a machine cannot
participate. This is framework plumbing, not the agent SDK.
"""

from __future__ import annotations

import re
import subprocess

from base.host.system.hostport import Host

__all__ = ["github_auth_blocker", "github_permission_blocker", "memory_repo_slug"]

# Each `gh` subprocess gets a bounded wait so a hung/offline call surfaces as a
# blocker reason instead of stalling `meiva start`.
_GH_TIMEOUT = 20

# A viewerPermission at or above WRITE can push branches and merge PRs.
_WRITE_OR_BETTER = frozenset({"WRITE", "MAINTAIN", "ADMIN"})

# SSH form: git@github.com:OWNER/NAME(.git) ; https form: https://github.com/OWNER/NAME(.git)
_SSH_RE = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<name>.+?)(?:\.git)?$")
_HTTPS_RE = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<name>.+?)(?:\.git)?$")


def memory_repo_slug(remote: str) -> str:
    """Derive the memory repo's `owner/name` slug from its git remote URL.

    Parses both the SSH form `git@github.com:OWNER/NAME.git` and the https form
    `https://github.com/OWNER/NAME(.git)`, stripping a trailing `.git`.

    Raises:
        ValueError: the remote URL is not a GitHub remote (no silent default).
    """
    url = remote
    m = _SSH_RE.match(url) or _HTTPS_RE.match(url)
    if m is None:
        raise ValueError(f"not a GitHub remote: {url!r}")
    return f"{m['owner']}/{m['name']}"


def github_auth_blocker(*, host: Host) -> str | None:
    # meiva:note
    # Report a missing or unauthenticated gh CLI before remote configuration is needed.
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if host.which("gh") is None:
        return "gh CLI not installed"

    try:
        auth = host.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "gh auth status timed out"
    if auth.returncode != 0:
        return "gh is not authenticated to github.com (run `gh auth login`)"

    return None


def github_permission_blocker(*, host: Host, slug: str) -> str | None:
    # meiva:note
    # Report whether the authenticated gh account can write the selected repository.
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    try:
        perm = host.run(
            ["gh", "repo", "view", slug, "--json", "viewerPermission", "-q", ".viewerPermission"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"reading permission on {slug} via gh timed out"
    if perm.returncode != 0:
        return f"could not read permission on {slug} via gh"
    value = perm.stdout.strip()
    if not value:
        return f"could not read permission on {slug} via gh"
    if value not in _WRITE_OR_BETTER:
        return f"gh account lacks write access to {slug} (viewerPermission={value!r})"

    return None

"""Trunk merge-queue HTTP API and PR base-freshness helpers shared by the CI tools."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request

from base.host.git.gitenv import git_env
from base.host.net.ports import UrlOpener
from base.host.system.hostport import Host

API_BASE_URL = "https://api.trunk.io/v1"
REQUEST_TIMEOUT_SECONDS = 30
ORG_SLUG = "meiva"


def target_payload(repo: str) -> dict[str, object]:
    """Build the repository and target branch identity Trunk endpoints share."""
    owner, name = repo.split("/", maxsplit=1)
    return {
        "repo": {"host": "github.com", "owner": owner, "name": name},
        "targetBranch": "main",
    }


def pr_payload(pr: str, repo: str) -> dict[str, object]:
    """Build the repository, PR, and target branch identity Trunk requires."""
    payload = target_payload(repo)
    payload["pr"] = {"number": int(pr)}
    return payload


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_sha(value: str) -> bool:
    return bool(_SHA1_RE.match(value) or _SHA256_RE.match(value))


# meiva:note
# Every failure the caller cannot act on reads as None: a timeout, a program that cannot start, a
# non-zero exit and non-SHA output. An unexpected exception still propagates, so a broken command
# implementation never passes for a missing answer.
def _sha_from(argv: list[str], *, host: Host, env: dict[str, str] | None = None) -> str | None:
    try:
        result = host.run(
            argv, env=env, capture_output=True, text=True, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    stdout = result.stdout.strip()
    sha = stdout.split(maxsplit=1)[0] if stdout else ""
    return sha if _is_sha(sha) else None


# meiva:note
# GitHub is the queue's truth for main; a checkout's origin is not evidence, because a managed mirror
# that stops syncing answers with an old main and makes every stale base look current. gh api is the
# authenticated read, the public ls-remote covers a missing or unauthenticated gh, and the repo
# argument supplies owner and name for both — no remote name, no second repository configuration.
def _main_sha(repo: str, *, host: Host) -> str | None:
    sha = _sha_from(
        ["gh", "api", f"repos/{repo}/git/ref/heads/main", "--jq", ".object.sha"], host=host
    )
    if sha is not None:
        return sha
    return _sha_from(
        ["git", "ls-remote", f"https://github.com/{repo}.git", "refs/heads/main"],
        host=host,
        env=git_env(),
    )


# meiva:note
# Returns ((base_sha, main_sha), False) when the PR's recorded baseRefOid differs from main,
# (None, False) when they match, and (None, True) when either read is unknown. Advisory: an unknown
# never blocks --merge, and --require-fresh-base refuses only on a stale base.
def base_freshness(pr: str, repo: str, *, host: Host) -> tuple[tuple[str, str] | None, bool]:
    base_sha = _sha_from(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "baseRefOid", "--jq", ".baseRefOid"],
        host=host,
    )
    if base_sha is None:
        return None, True
    main_sha = _main_sha(repo, host=host)
    if main_sha is None:
        return None, True
    if base_sha == main_sha:
        return None, False
    return (base_sha, main_sha), False


def post(
    endpoint: str,
    payload: dict[str, object],
    token: str,
    *,
    open_url: UrlOpener,
) -> tuple[dict[str, object] | None, str | None]:
    """POST one Trunk API request, returning its object response or an error."""
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS Trunk API endpoint
        f"{API_BASE_URL}/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-token": token},
        method="POST",
    )
    try:
        with open_url(request, REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        # urllib raises HTTPError (a URLError subclass) instead of returning
        # the response for 4xx/5xx, and its str() is "HTTP Error 409: ..." —
        # normalize to "HTTP <code>" so callers can match statuses exactly.
        return None, f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, str(error)
    if status != 200:
        return None, f"HTTP {status}"
    if not body:
        return {}, None
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # submitPullRequest / cancelPullRequest answer 200 with a plain-text
        # "OK" body (verified live 2026-09-01); a 200 is a success regardless
        # of body shape, so a non-JSON body must not be read as an error.
        return {}, None
    if not isinstance(data, dict):
        return None, "response was not a JSON object"
    return data, None

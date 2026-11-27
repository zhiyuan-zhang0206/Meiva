from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from scripts.codegen.build_app_update_manifest import semver_tuple
from scripts.host_ops.release.github_releases import read_release_field, run_gh

_CLIENT_TAG_PREFIX = "client-v"


def check_client_release(gh: Callable[..., str], version: str) -> None:
    candidate = semver_tuple(version)
    tag = f"{_CLIENT_TAG_PREFIX}{version}"
    matching_releases = gh(
        "api",
        "--paginate",
        "repos/{owner}/{repo}/releases",
        "--jq",
        f'.[] | select(.tag_name == "{tag}") | .id',
    )
    if matching_releases.strip():
        raise ValueError(f"refusing to modify existing release {tag}")
    latest = read_release_field(gh, "latest", "tag_name")
    if (
        latest is not None
        and latest.startswith(_CLIENT_TAG_PREFIX)
        and candidate <= semver_tuple(latest.removeprefix(_CLIENT_TAG_PREFIX))
    ):
        raise ValueError(f"refusing to make {tag} Latest: {latest} already holds Latest")


class _Arguments(argparse.Namespace):
    version: str


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Check a new client release before publication.")
    _ = parser.add_argument("version")
    args = parser.parse_args(argv, namespace=_Arguments())
    check_client_release(run_gh, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

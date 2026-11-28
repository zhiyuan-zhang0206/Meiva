from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from scripts.host_ops.release.client_release_baseline import (
    ANDROID_MANIFEST,
    APP_MANIFEST,
    CLIENT_TAG_PREFIX,
    check_latest,
)
from scripts.host_ops.release.github_releases import (
    asset_digest,
    read_release_field,
    run_gh,
    uploaded_assets,
)


def _draft_id(gh: Callable[..., str], tag: str) -> str | None:
    rows = gh(
        "api",
        "--paginate",
        "repos/{owner}/{repo}/releases",
        "--jq",
        f'.[] | select(.tag_name == "{tag}") | [.id, .draft] | @tsv',
    ).splitlines()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError(f"multiple releases match {tag}")
    release_id, draft = rows[0].split("\t")
    if draft != "true":
        raise ValueError(f"refusing to modify published release {tag}")
    return release_id


def _check_latest_snapshot(gh: Callable[..., str], version: str, expected: str) -> None:
    current = read_release_field(gh, "latest", "tag_name") or ""
    check_latest(version, current)
    if current != expected:
        raise ValueError(
            f"Latest changed from {expected!r} to {current!r} while staging the release"
        )


def _expected_assets(directory: Path) -> dict[str, str]:
    files = sorted(directory.iterdir())
    expected = {path.name: asset_digest(path) for path in files if path.stat().st_size}
    if (
        len(files) != 6
        or len(expected) != 6
        or not {APP_MANIFEST, ANDROID_MANIFEST} <= expected.keys()
    ):
        raise ValueError("expected six non-empty client release assets including both manifests")
    return expected


def publish_client_release(
    gh: Callable[..., str], *, version: str, directory: Path, latest: str, notes: str
) -> None:
    _check_latest_snapshot(gh, version, latest)
    tag = f"{CLIENT_TAG_PREFIX}{version}"
    expected = _expected_assets(directory)

    release_id = _draft_id(gh, tag)
    if release_id is None:
        _ = gh(
            "release",
            "create",
            tag,
            "--verify-tag",
            "--draft",
            "--title",
            f"Meiva App {version}",
            "--notes",
            notes,
        )
        release_id = _draft_id(gh, tag)
        if release_id is None:
            raise ValueError(f"created draft {tag} is missing")

    uploaded = uploaded_assets(gh, release_id)
    if any(expected.get(name) != digest for name, digest in uploaded.items()):
        raise ValueError(f"draft {tag} contains unexpected assets or digests")
    missing = [str(directory / name) for name in sorted(expected.keys() - uploaded.keys())]
    if missing:
        _ = gh("release", "upload", tag, *missing)
    if uploaded_assets(gh, release_id) != expected:
        raise ValueError(f"draft {tag} does not have the six expected remote SHA-256 digests")

    _check_latest_snapshot(gh, version, latest)
    if _draft_id(gh, tag) != release_id:
        raise ValueError(f"draft {tag} changed during upload")
    _ = gh("release", "edit", tag, "--draft=false", "--latest")


class _Arguments(argparse.Namespace):
    version: str
    directory: Path
    latest: str
    notes: Path


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a verified six-asset client draft as Latest."
    )
    _ = parser.add_argument("--version", required=True)
    _ = parser.add_argument("--directory", required=True, type=Path)
    _ = parser.add_argument("--latest", required=True)
    _ = parser.add_argument("--notes", required=True, type=Path)
    args = parser.parse_args(argv, namespace=_Arguments())
    publish_client_release(
        run_gh,
        version=args.version,
        directory=args.directory,
        latest=args.latest,
        notes=args.notes.read_text(encoding="utf-8"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

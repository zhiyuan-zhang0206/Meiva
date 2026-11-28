from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlencode

from scripts.host_ops.release.client_release_baseline import (
    ANDROID_MANIFEST,
    APP_MANIFEST,
    CLIENT_TAG_PREFIX,
    check_latest,
)
from scripts.host_ops.release.github_releases import (
    asset_digest,
    read_release_field,
    read_release_query,
    release_assets,
    run_gh,
)

_RELEASES = "repos/{owner}/{repo}/releases"


def _find_existing_draft(gh: Callable[..., str], tag: str) -> str | None:
    rows = gh(
        "api",
        "--paginate",
        _RELEASES,
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


def _create_draft(gh: Callable[..., str], tag: str, version: str, notes: str) -> tuple[str, str]:
    ref = gh("api", f"repos/{{owner}}/{{repo}}/git/ref/tags/{tag}", "--jq", ".ref")
    if ref.strip() != f"refs/tags/{tag}":
        raise ValueError(f"could not verify existing tag {tag}")
    row = gh(
        "api",
        "--method",
        "POST",
        _RELEASES,
        "-f",
        f"tag_name={tag}",
        "-f",
        f"name=Meiva App {version}",
        "-f",
        f"body={notes}",
        "-F",
        "draft=true",
        "--jq",
        "[.id, .upload_url] | @tsv",
    )
    release_id, upload_url = row.rstrip("\n").split("\t")
    if not release_id.isascii() or not release_id.isdecimal():
        raise ValueError(f"GitHub returned an invalid release ID for {tag}")
    return release_id, upload_url


def _require_draft(gh: Callable[..., str], tag: str, release_id: str) -> str:
    row = read_release_query(gh, release_id, "[.tag_name, .draft, .upload_url] | @tsv")
    if row is None:
        raise ValueError(f"draft {tag} (release {release_id}) is missing")
    actual_tag, draft, upload_url = row.split("\t")
    if actual_tag != tag:
        raise ValueError(f"release {release_id} changed tag from {tag} to {actual_tag}")
    if draft != "true":
        raise ValueError(f"refusing to modify published release {tag}")
    return upload_url


def _upload_assets(gh: Callable[..., str], upload_url: str, files: Sequence[Path]) -> None:
    endpoint = upload_url.partition("{")[0]
    if not endpoint.startswith("https://"):
        raise ValueError(f"GitHub returned an invalid release upload URL: {upload_url!r}")
    for path in files:
        query = urlencode({"name": path.name})
        _ = gh(
            "api",
            "--method",
            "POST",
            f"{endpoint}?{query}",
            "--header",
            "Content-Type: application/octet-stream",
            "--input",
            str(path),
        )


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


def _resume_draft(
    gh: Callable[..., str], tag: str, release_id: str, expected: dict[str, str]
) -> tuple[str, set[str]]:
    assets = release_assets(gh, release_id)
    if any(
        asset.state != "starter"
        and (asset.state != "uploaded" or asset.digest != expected.get(name))
        for name, asset in assets.items()
    ):
        raise ValueError(f"draft {tag} contains unexpected assets or digests")
    upload_url = _require_draft(gh, tag, release_id)
    for asset in assets.values():
        if asset.state == "starter":
            _ = gh("api", "--method", "DELETE", f"{_RELEASES}/assets/{asset.id}")
    return upload_url, {name for name, asset in assets.items() if asset.state == "uploaded"}


def publish_client_release(
    gh: Callable[..., str], *, version: str, directory: Path, latest: str, notes: str
) -> None:
    _check_latest_snapshot(gh, version, latest)
    tag = f"{CLIENT_TAG_PREFIX}{version}"
    expected = _expected_assets(directory)

    release_id = _find_existing_draft(gh, tag)
    uploaded: set[str] = set()
    if release_id is None:
        release_id, upload_url = _create_draft(gh, tag, version, notes)
    else:
        upload_url, uploaded = _resume_draft(gh, tag, release_id, expected)

    missing = [directory / name for name in sorted(expected.keys() - uploaded)]
    if missing:
        _upload_assets(gh, upload_url, missing)
    remote = release_assets(gh, release_id)
    if len(remote) != len(expected) or any(
        asset.state != "uploaded" or asset.digest != expected.get(name)
        for name, asset in remote.items()
    ):
        raise ValueError(f"draft {tag} does not have the six expected remote SHA-256 digests")

    _check_latest_snapshot(gh, version, latest)
    _ = _require_draft(gh, tag, release_id)
    _ = gh(
        "api",
        "--method",
        "PATCH",
        f"{_RELEASES}/{release_id}",
        "-F",
        "draft=false",
        "-f",
        "make_latest=true",
    )


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

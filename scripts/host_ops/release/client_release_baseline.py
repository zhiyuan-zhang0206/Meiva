from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.codegen.build_app_update_manifest import semver_tuple
from scripts.host_ops.release.github_releases import (
    asset_rows,
    download_asset,
    read_release_query,
    run_gh,
)

APP_MANIFEST = "latest.json"
ANDROID_MANIFEST = "android-latest.json"
CLIENT_TAG_PREFIX = "client-v"


@dataclass(frozen=True)
class ReleaseAssets:
    tag: str
    digests: dict[str, str]

    def download(self, gh: Callable[..., str], path: Path) -> None:
        digest = self.digests.get(path.name)
        if not digest:
            raise ValueError(f"{self.tag} is missing the digest for {path.name}")
        download_asset(gh, self.tag, path, digest)


@dataclass(frozen=True)
class ClientBaseline:
    latest_tag: str
    app: ReleaseAssets | None
    android: ReleaseAssets | None


def _release(gh: Callable[..., str], selector: str) -> ReleaseAssets | None:
    rows = read_release_query(
        gh,
        selector,
        '.tag_name, (.assets[] | select(.state == "uploaded") | [.name, .digest] | @tsv)',
    )
    if rows is None:
        return None
    tag, _, assets = rows.partition("\n")
    return ReleaseAssets(tag, asset_rows(assets))


def _manifest_source(
    gh: Callable[..., str], latest: ReleaseAssets | None, name: str, legacy_tag: str
) -> ReleaseAssets | None:
    if latest is not None and name in latest.digests:
        return latest
    legacy = _release(gh, f"tags/{legacy_tag}")
    return legacy if legacy is not None and name in legacy.digests else None


def fetch_baseline(gh: Callable[..., str], directory: Path) -> ClientBaseline:
    latest = _release(gh, "latest")
    app = _manifest_source(gh, latest, APP_MANIFEST, "app-latest")
    android = _manifest_source(gh, latest, ANDROID_MANIFEST, "android-latest")
    directory.mkdir(parents=True, exist_ok=True)
    for source, name in ((app, APP_MANIFEST), (android, ANDROID_MANIFEST)):
        if source is not None:
            source.download(gh, directory / name)
    return ClientBaseline(latest.tag if latest is not None else "", app, android)


def check_latest(version: str, latest: str) -> None:
    candidate = semver_tuple(version)
    if latest.startswith(CLIENT_TAG_PREFIX) and candidate <= semver_tuple(
        latest.removeprefix(CLIENT_TAG_PREFIX)
    ):
        raise ValueError(
            f"refusing to make {CLIENT_TAG_PREFIX}{version} Latest: {latest} already holds Latest"
        )


class _Arguments(argparse.Namespace):
    directory: Path


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Fetch the current client update manifests.")
    _ = parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv, namespace=_Arguments())
    _ = fetch_baseline(run_gh, args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

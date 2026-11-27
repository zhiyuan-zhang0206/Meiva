from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep

from scripts.codegen.build_android_release import assert_version_code_advances
from scripts.codegen.build_app_update_manifest import assert_version_advances
from scripts.host_ops.release.github_releases import read_release_field, run_gh


class _Arguments(argparse.Namespace):
    tag: str
    target: str
    title: str
    notes: str
    version: str | None
    version_code: int | None
    manifest_attempts: int
    files: list[Path]


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _uploaded_assets(gh: Callable[..., str], release_id: str) -> dict[str, str]:
    rows = gh(
        "api",
        "--paginate",
        f"repos/{{owner}}/{{repo}}/releases/{release_id}/assets",
        "--jq",
        '.[] | [.name, (if .state == "uploaded" then .digest else "" end)] | @tsv',
    )
    return dict(line.split("\t", maxsplit=1) for line in rows.splitlines())


def _check_current_manifest(
    gh: Callable[..., str],
    tag: str,
    manifest_name: str,
    current_digest: str,
    assert_advances: Callable[[Path], None],
) -> None:
    with TemporaryDirectory() as directory:
        previous = Path(directory) / manifest_name
        _ = gh("release", "download", tag, "--pattern", manifest_name, "--output", str(previous))
        if _digest(previous) != current_digest:
            raise ValueError(f"{tag} returned an unverified {manifest_name}")
        assert_advances(previous)


def _replace_manifest(
    gh: Callable[..., str],
    wait: Callable[[float], None],
    tag: str,
    manifest: Path,
    attempts: int,
) -> None:
    for delay in range(1, attempts):
        try:
            _ = gh("release", "upload", tag, str(manifest), "--clobber")
        except subprocess.CalledProcessError:
            wait(delay)
        else:
            return
    _ = gh("release", "upload", tag, str(manifest), "--clobber")


def _upload_binaries(
    gh: Callable[..., str],
    tag: str,
    binaries: Sequence[Path],
    uploaded: dict[str, str],
    expected: dict[str, str],
) -> None:
    for binary in binaries:
        if binary.name in uploaded and uploaded[binary.name] != expected[binary.name]:
            raise ValueError(f"{tag} already contains a different {binary.name}")
    missing = [str(binary) for binary in binaries if binary.name not in uploaded]
    if missing:
        _ = gh("release", "upload", tag, *missing)


def _verify_assets(tag: str, expected: dict[str, str], uploaded: dict[str, str]) -> None:
    if any(uploaded.get(name) != digest for name, digest in expected.items()):
        raise ValueError(f"{tag} is missing one or more assets with the expected digest")


def publish_feed(
    gh: Callable[..., str],
    *,
    tag: str,
    files: Sequence[Path],
    title: str,
    notes: str,
    target: str,
    assert_advances: Callable[[Path], None],
    manifest_attempts: int,
    wait: Callable[[float], None],
) -> None:
    if len(files) < 2 or any(path.stat().st_size == 0 for path in files):
        raise ValueError("expected non-empty binary assets and a manifest")
    if manifest_attempts < 1:
        raise ValueError("manifest upload requires at least one attempt")
    manifest = files[-1]
    expected = {path.name: _digest(path) for path in files}
    release_id = read_release_field(gh, f"tags/{tag}", "id")
    if release_id is None:
        _ = gh(
            "release",
            "create",
            tag,
            "--target",
            target,
            "--prerelease",
            "--latest=false",
            "--title",
            title,
            "--notes",
            notes,
        )
        release_id = read_release_field(gh, f"tags/{tag}", "id")
        if release_id is None:
            raise ValueError(f"created release {tag} is missing")

    uploaded = _uploaded_assets(gh, release_id)
    current_digest = uploaded.get(manifest.name)
    manifest_changed = current_digest != expected[manifest.name]
    if current_digest is not None and manifest_changed:
        _check_current_manifest(gh, tag, manifest.name, current_digest, assert_advances)
    binaries = files[:-1]
    _upload_binaries(gh, tag, binaries, uploaded, expected)
    if manifest_changed:
        _verify_assets(
            tag,
            {path.name: expected[path.name] for path in binaries},
            _uploaded_assets(gh, release_id),
        )
        _replace_manifest(gh, wait, tag, manifest, manifest_attempts)

    uploaded = _uploaded_assets(gh, release_id)
    _verify_assets(tag, expected, uploaded)
    for name in sorted(uploaded.keys() - expected.keys()):
        _ = gh("release", "delete-asset", tag, name, "--yes")
    _ = gh(
        "release", "edit", tag, "--prerelease", "--latest=false", "--title", title, "--notes", notes
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Publish and reconcile a mutable client updater feed."
    )
    _ = parser.add_argument("--tag", required=True, choices=["app-latest", "android-latest"])
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--title", required=True)
    _ = parser.add_argument("--notes", required=True)
    _ = parser.add_argument("--manifest-attempts", required=True, type=int)
    version = parser.add_mutually_exclusive_group(required=True)
    _ = version.add_argument("--version")
    _ = version.add_argument("--version-code", type=int)
    _ = parser.add_argument(
        "files", type=Path, nargs="+", help="binary assets followed by the manifest"
    )
    args = parser.parse_args(argv, namespace=_Arguments())

    if args.tag == "app-latest":
        candidate = args.version
        if candidate is None:
            parser.error("app-latest requires --version")

        def assert_advances(previous: Path) -> None:
            assert_version_advances(current=previous, version=candidate)

    else:
        candidate_code = args.version_code
        if candidate_code is None:
            parser.error("android-latest requires --version-code")

        def assert_advances(previous: Path) -> None:
            assert_version_code_advances(current=previous, version_code=candidate_code)

    publish_feed(
        run_gh,
        tag=args.tag,
        files=args.files,
        title=args.title,
        notes=args.notes,
        target=args.target,
        assert_advances=assert_advances,
        manifest_attempts=args.manifest_attempts,
        wait=sleep,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

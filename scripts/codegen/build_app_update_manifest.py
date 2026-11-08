#!/usr/bin/env python3
"""Build Tauri's static ``latest.json`` from app release artifacts.

The desktop updater consumes signed ``.app.tar.gz`` archives, not the
user-facing DMG installer. A release built without the optional updater key
still gets a valid manifest with no platform entries: the release pipeline
remains testable, while clients correctly find no installable update rather
than being handed an unverifiable archive.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict, TypeGuard
from urllib.parse import quote

LATEST_RELEASE_TAG = "app-latest"
_TAG = re.compile(r"app-v(?P<version>\d+\.\d+\.\d+)\Z")
_SEMVER = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def _semver_tuple(version: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(f"'{version}' is not a <major>.<minor>.<patch> version")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def build_version_name(cargo_version: str, run_number: int) -> str:
    if run_number < 1:
        raise ValueError(f"run number must be positive, got {run_number}")
    major, minor, patch = _semver_tuple(cargo_version)
    return f"{major}.{minor}.{patch + run_number}"


def _decoded(decode: Callable[[str], object], text: str) -> object:
    return decode(text)


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def assert_version_advances(*, current: Path, version: str) -> None:
    candidate = _semver_tuple(version)
    body = _decoded(json.loads, current.read_text(encoding="utf-8"))
    if not _is_str_object_dict(body):
        raise TypeError(f"{current} is not a manifest object")
    previous_raw = body.get("version")
    if not isinstance(previous_raw, str):
        raise TypeError(f"{current} has no string version")
    previous = _semver_tuple(previous_raw)
    if candidate <= previous:
        raise ValueError(
            f"refusing version {version}: {LATEST_RELEASE_TAG} already advertises {previous_raw}"
        )


def _signed_archive(artifacts: Path, pattern: str, label: str) -> tuple[Path, str] | None:
    signed: list[tuple[Path, str]] = []
    for archive in sorted(artifacts.glob(pattern)):
        signature_path = archive.with_name(f"{archive.name}.sig")
        if not signature_path.is_file():
            continue
        signature = signature_path.read_text(encoding="utf-8").strip()
        if signature:
            signed.append((archive, signature))
    if len(signed) > 1:
        names = ", ".join(archive.name for archive, _ in signed)
        raise ValueError(f"multiple {label} updater archives: {names}")
    return signed[0] if signed else None


def _platform_entry(repo: str, tag: str, archive: Path, signature: str) -> dict[str, str]:
    asset = quote(archive.name, safe="")
    return {
        "url": f"https://github.com/{repo}/releases/download/{tag}/{asset}",
        "signature": signature,
    }


class AppUpdateManifest(TypedDict):
    # meiva:note
    # Tauri's updater reads `version` and `platforms` from the manifest; `commit` records the Git SHA packaged into `app-latest` so the release train knows whether `ui/app` has changed since the last published desktop build.

    version: str
    platforms: dict[str, dict[str, str]]
    commit: NotRequired[str]


# meiva:note
# The parsed flags as a typed namespace. The release workflow runs this file with the runner's bare
# `python3`, so it takes no project import: the flags are read through the namespace's own
# annotations, which argparse fills.
class _Arguments(argparse.Namespace):
    command: str | None
    cargo_version: str
    run_number: int
    current: Path
    tag: str
    version: str | None
    commit: str | None
    artifacts: Path
    repo: str
    out: Path


def _resolve_manifest_version(tag: str, version: str | None) -> str:
    if tag == LATEST_RELEASE_TAG:
        if version is None:
            raise ValueError(f"--version is required when --tag is '{LATEST_RELEASE_TAG}'")
        _semver_tuple(version)
        return version
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"'{tag}' is not an app-v<major>.<minor>.<patch> tag")
    tag_version = match.group("version")
    if version is not None and version != tag_version:
        raise ValueError(f"--version '{version}' does not match tag '{tag}'")
    return tag_version


def build_manifest(
    *,
    tag: str,
    artifacts: Path,
    repo: str,
    version: str | None = None,
    commit: str | None = None,
) -> AppUpdateManifest:
    resolved_version = _resolve_manifest_version(tag, version)
    if commit is not None and _COMMIT.fullmatch(commit) is None:
        raise ValueError(f"'{commit}' is not a full commit SHA")
    if len(repo.split("/")) != 2 or any(not part for part in repo.split("/")):
        raise ValueError(f"'{repo}' is not an owner/repository name")
    if not artifacts.is_dir():
        raise ValueError(f"artifact directory does not exist: {artifacts}")

    platforms: dict[str, dict[str, str]] = {}
    macos = _signed_archive(artifacts, "*.app.tar.gz", "macOS")
    if macos is not None:
        archive, signature = macos
        entry = _platform_entry(repo, tag, archive, signature)
        # The universal archive contains both slices; the updater identifies
        # the running slice, so both target keys point at the same asset.
        platforms["darwin-aarch64"] = entry
        platforms["darwin-x86_64"] = entry

    manifest: AppUpdateManifest = {"version": resolved_version, "platforms": platforms}
    if commit is not None:
        manifest["commit"] = commit
    return manifest


def _add_manifest_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", default=None)
    parser.add_argument("--commit", default=None)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", required=True, type=Path)


def main(argv: list[str]) -> int:
    if argv and argv[0] in {"version-name", "assert-advances", "manifest"}:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)

        version_parser = commands.add_parser(
            "version-name", help="print the monotonic SemVer for a workflow run"
        )
        version_parser.add_argument("--cargo-version", required=True)
        version_parser.add_argument("--run-number", type=int, required=True)

        guard_parser = commands.add_parser(
            "assert-advances", help="fail unless the version is newer than app-latest's manifest"
        )
        guard_parser.add_argument("--current", type=Path, required=True)
        guard_parser.add_argument("--version", required=True)

        manifest_parser = commands.add_parser("manifest", help="write latest.json")
        _add_manifest_arguments(manifest_parser)

        args = parser.parse_args(argv, namespace=_Arguments())
        if args.command == "version-name":
            print(build_version_name(args.cargo_version, args.run_number))
            return 0
        if args.command == "assert-advances":
            if args.version is None:
                raise ValueError("--version is required")
            assert_version_advances(current=args.current, version=args.version)
            print(f"version {args.version} advances {LATEST_RELEASE_TAG}")
            return 0
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        _add_manifest_arguments(parser)
        args = parser.parse_args(argv, namespace=_Arguments())

    manifest = build_manifest(
        tag=args.tag,
        artifacts=args.artifacts,
        repo=args.repo,
        version=args.version,
        commit=args.commit,
    )
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

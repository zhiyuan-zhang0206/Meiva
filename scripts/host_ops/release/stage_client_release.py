from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.options import options
from base.host.boundary.untyped import object_of
from scripts.codegen import build_android_release, build_app_update_manifest
from scripts.host_ops.release.client_release_baseline import (
    ANDROID_MANIFEST,
    APP_MANIFEST,
    CLIENT_TAG_PREFIX,
    ReleaseAssets,
    check_latest,
    fetch_baseline,
)
from scripts.host_ops.release.github_releases import run_gh


@dataclass(frozen=True)
class Build:
    version: str
    commit: str


@dataclass(frozen=True)
class AndroidBuild(Build):
    version_code: int
    built_at: str


@dataclass(frozen=True)
class ReleaseTarget:
    repo: str
    tag: str
    directory: Path


def _manifest(path: Path) -> dict[str, object]:
    body = object_of(json.loads, path.read_text(encoding="utf-8"))
    if not is_object_dict(body):
        raise ValueError(f"{path} must contain a manifest object")
    return body


def _text(body: dict[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"manifest requires a non-empty {name}")
    return value


def _stage_asset(
    gh: Callable[..., str], source: ReleaseAssets | None, artifacts: Path, path: Path
) -> None:
    if source is None:
        _ = shutil.copyfile(artifacts / path.name, path)
    else:
        source.download(gh, path)


def _check_carried_app_signature(
    baseline: dict[str, object], targets: Iterable[str], signature: Path
) -> None:
    platforms = baseline.get("platforms")
    downloaded = signature.read_bytes().decode("utf-8")
    for target in targets:
        entry = platforms.get(target) if is_object_dict(platforms) else None
        if not is_object_dict(entry) or entry.get("signature") != downloaded:
            raise ValueError(
                f"{signature.name} does not match the prior macOS manifest for {target}"
            )


def _stage_app(
    gh: Callable[..., str],
    release: ReleaseTarget,
    build: Build,
    previous: ReleaseAssets | None,
    baseline: Path,
    artifacts: Path,
    tauri_config: Path,
) -> str:
    source = None
    prior_manifest = None
    if not build.version:
        if previous is None:
            raise ValueError("macOS needs a fresh build because Latest has no macOS baseline")
        prior_manifest = _manifest(baseline)
        build = Build(_text(prior_manifest, "version"), _text(prior_manifest, "commit"))
        source = previous
    elif previous is not None:
        build_app_update_manifest.assert_version_advances(current=baseline, version=build.version)

    _ = build_app_update_manifest.semver_tuple(build.version)
    archive = f"Meiva_{build.version}.app.tar.gz"
    for name in (f"Meiva_{build.version}_universal.dmg", archive, f"{archive}.sig"):
        _stage_asset(gh, source, artifacts, release.directory / name)
    manifest = build_app_update_manifest.build_manifest(
        tag=release.tag,
        version=build.version,
        commit=build.commit,
        artifacts=release.directory,
        repo=release.repo,
        tauri_config=tauri_config,
    )
    if not manifest["platforms"]:
        raise ValueError("macOS build is missing its updater signature")
    if prior_manifest is not None:
        _check_carried_app_signature(
            prior_manifest, manifest["platforms"], release.directory / f"{archive}.sig"
        )
    _write_manifest(release.directory / APP_MANIFEST, manifest)
    return build.version


def _stage_android(
    gh: Callable[..., str],
    release: ReleaseTarget,
    build: AndroidBuild,
    previous: ReleaseAssets | None,
    baseline: Path,
    artifacts: Path,
) -> str:
    source = None
    prior_digest = None
    if previous is not None:
        body = _manifest(baseline)
        version = _text(body, "versionName")
        if build.version:
            build_android_release.assert_version_code_advances(
                current=baseline,
                version_code=build.version_code,
            )
            if build_app_update_manifest.semver_tuple(
                build.version
            ) < build_app_update_manifest.semver_tuple(version):
                raise ValueError(f"Android version {build.version} would regress from {version}")
        else:
            code = body.get("versionCode")
            if isinstance(code, bool) or not isinstance(code, int):
                raise ValueError("manifest requires an integer versionCode")
            build = AndroidBuild(version, _text(body, "commit"), code, _text(body, "builtAt"))
            prior_digest = _text(body, "sha256")
            source = previous
    elif not build.version:
        raise ValueError("Android needs a fresh build because Latest has no Android baseline")

    _ = build_app_update_manifest.semver_tuple(build.version)
    apk = release.directory / f"meiva-app-{build.version}.apk"
    _stage_asset(gh, source, artifacts, apk)
    manifest = build_android_release.build_manifest(
        tag=release.tag,
        version_code=build.version_code,
        version_name=build.version,
        apk=apk,
        commit=build.commit,
        repo=release.repo,
        built_at=build.built_at,
    )
    if prior_digest is not None and manifest["sha256"] != prior_digest:
        raise ValueError(f"{apk.name} does not match the prior Android manifest")
    _write_manifest(release.directory / ANDROID_MANIFEST, manifest)
    return build.version


def _write_manifest(path: Path, manifest: object) -> None:
    _ = path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class StageOptions:
    app_version: str
    android_version: str
    android_code: int
    android_built_at: str
    commit: str
    repo: str
    artifacts: Path
    workspace: Path
    github_output: Path


def stage_client_release(
    gh: Callable[..., str],
    args: StageOptions,
    *,
    tauri_config: Path = build_app_update_manifest.DEFAULT_TAURI_CONFIG,
) -> None:
    version = args.app_version or args.android_version
    if args.app_version and args.android_version and args.app_version != args.android_version:
        raise ValueError("the two new builds disagree on the client version")
    inputs = args.workspace / "baseline"
    baseline = fetch_baseline(gh, inputs)
    check_latest(version, baseline.latest_tag)

    release = ReleaseTarget(args.repo, f"{CLIENT_TAG_PREFIX}{version}", args.workspace / "assets")
    release.directory.mkdir()
    app_version = _stage_app(
        gh,
        release,
        Build(args.app_version, args.commit),
        baseline.app,
        inputs / APP_MANIFEST,
        args.artifacts,
        tauri_config,
    )
    android_version = _stage_android(
        gh,
        release,
        AndroidBuild(args.android_version, args.commit, args.android_code, args.android_built_at),
        baseline.android,
        inputs / ANDROID_MANIFEST,
        args.artifacts,
    )
    _ = (args.workspace / "notes.md").write_text(
        f"macOS Universal {app_version} and Android {android_version}.\n\n"
        f"Release train commit: `{args.commit}`\n",
        encoding="utf-8",
    )
    with args.github_output.open("a", encoding="utf-8") as output:
        _ = output.write(f"version={version}\nlatest={baseline.latest_tag}\n")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Stage a complete client release from builds and Latest."
    )
    _ = parser.add_argument("--app-version", required=True)
    _ = parser.add_argument("--android-version", required=True)
    _ = parser.add_argument("--android-code", required=True, type=int)
    _ = parser.add_argument("--android-built-at", required=True)
    _ = parser.add_argument("--commit", required=True)
    _ = parser.add_argument("--repo", required=True)
    _ = parser.add_argument("--artifacts", required=True, type=Path)
    _ = parser.add_argument("--workspace", required=True, type=Path)
    _ = parser.add_argument("--github-output", required=True, type=Path)
    stage_client_release(run_gh, options(parser.parse_args(argv), StageOptions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

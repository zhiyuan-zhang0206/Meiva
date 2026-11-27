from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import pytest

from scripts.codegen.build_android_release import assert_version_code_advances
from scripts.codegen.build_app_update_manifest import assert_version_advances
from scripts.host_ops.release.publish_feed import publish_feed


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class _Release:
    assets: dict[str, bytes]
    calls: list[tuple[str, ...]] = field(default_factory=list)
    fail_binary: bool = False
    fail_download: bool = False
    manifest_failures: int = 0
    corrupt_binary: bool = False
    corrupt_manifest: bool = False

    def gh(self, *args: str) -> str:
        self.calls.append(args)
        match args[:2]:
            case ("api", "--include"):
                return "HTTP/2.0 200 OK\n\n123\n"
            case ("api", "--paginate"):
                return "\n".join(f"{name}\t{_digest(data)}" for name, data in self.assets.items())
            case ("release", "download"):
                if self.fail_download:
                    raise subprocess.CalledProcessError(1, args)
                _ = Path(args[6]).write_bytes(self.assets[args[4]])
            case ("release", "upload"):
                self._upload(args[3:])
            case ("release", "delete-asset"):
                del self.assets[args[3]]
            case ("release", "edit"):
                pass
            case _:
                pytest.fail(f"unexpected GitHub command {args}")
        return ""

    def _upload(self, args: tuple[str, ...]) -> None:
        clobber = args[-1] == "--clobber"
        for path in map(Path, args[:-1] if clobber else args):
            if path.name.endswith(".json"):
                assert clobber
                _ = self.assets.pop(path.name, None)
                if self.manifest_failures:
                    self.manifest_failures -= 1
                    raise subprocess.CalledProcessError(1, args)
                corrupt = self.corrupt_manifest
            else:
                assert not clobber
                assert path.name not in self.assets
                if self.fail_binary:
                    raise subprocess.CalledProcessError(1, args)
                corrupt = self.corrupt_binary
            self.assets[path.name] = b"corrupt bytes" if corrupt else path.read_bytes()


@pytest.mark.parametrize("tag", ["app-latest", "android-latest"])
@pytest.mark.parametrize(
    "failure", ["none", "transient", "binary", "download", "manifest", "bad-binary", "bad-manifest"]
)
def test_feed_keeps_previous_binaries_until_remote_manifest_is_verified(
    tmp_path: Path, tag: str, failure: str
) -> None:
    if tag == "app-latest":
        archives = (
            "Meiva_1.0.2_universal.dmg",
            "Meiva_1.0.2.app.tar.gz",
            "Meiva_1.0.2.app.tar.gz.sig",
        )
        stale = ("Meiva_1.0.1_universal.dmg", "Meiva.app.tar.gz", "Meiva.app.tar.gz.sig")
        manifest = "latest.json"
        previous = b'{"version":"1.0.1"}'
        candidate = b'{"version":"1.0.2"}'
        assert_advances = partial(assert_version_advances, version="1.0.2")
    else:
        archives = ("meiva-app-1.0.2.apk",)
        stale = ("meiva-app-1.0.1.apk",)
        manifest = "android-latest.json"
        previous = b'{"versionCode":1000001}'
        candidate = b'{"versionCode":1000002}'
        assert_advances = partial(assert_version_code_advances, version_code=1000002)
    old_binaries = dict.fromkeys(stale, b"previous binary")
    release = _Release(
        assets={**old_binaries, manifest: previous},
        fail_binary=failure == "binary",
        fail_download=failure == "download",
        manifest_failures={"transient": 1, "manifest": 3}.get(failure, 0),
        corrupt_binary=failure == "bad-binary",
        corrupt_manifest=failure == "bad-manifest",
    )
    payloads = {**dict.fromkeys(archives, b"new binary"), manifest: candidate}
    files = tuple(tmp_path / name for name in payloads)
    for file in files:
        _ = file.write_bytes(payloads[file.name])
    waits: list[float] = []

    def publish() -> None:
        publish_feed(
            release.gh,
            tag=tag,
            version="1.0.2",
            files=files,
            title="Version 1.0.2",
            notes="Build metadata",
            target="a" * 40,
            manifest_attempts=3,
            wait=waits.append,
            assert_advances=lambda path: assert_advances(current=path),
        )

    if failure in {"none", "transient"}:
        publish()
        assert release.assets == payloads
        assert release.calls[-1][:3] == ("release", "edit", tag)
    else:
        with pytest.raises((subprocess.CalledProcessError, ValueError)):
            publish()
        assert old_binaries.items() <= release.assets.items()
        assert not any(
            call[:2] in {("release", "delete-asset"), ("release", "edit")} for call in release.calls
        )
    if failure in {"binary", "download", "bad-binary"}:
        assert release.assets[manifest] == previous
        assert not any("--clobber" in call for call in release.calls)
    assert waits == {"transient": [1], "manifest": [1, 2]}.get(failure, [])


def test_stale_app_version_fails_before_any_write(tmp_path: Path) -> None:
    old_archive = tmp_path / "Meiva_1.0.2.app.tar.gz"
    manifest = tmp_path / "latest.json"
    _ = old_archive.write_bytes(b"old package")
    _ = manifest.write_text('{"version":"1.0.2"}')
    assets = {
        "Meiva_1.0.161.app.tar.gz": b"current package",
        manifest.name: b'{"version":"1.0.161"}',
    }
    release = _Release(assets=dict(assets))

    with pytest.raises(ValueError, match=r"already advertises 1\.0\.161"):
        publish_feed(
            release.gh,
            tag="app-latest",
            version="1.0.2",
            files=(old_archive, manifest),
            title="Old tag",
            notes="",
            target="a" * 40,
            manifest_attempts=3,
            wait=lambda _: None,
            assert_advances=lambda path: assert_version_advances(current=path, version="1.0.2"),
        )

    assert release.assets == assets
    assert [command[:2] for command in release.calls] == [
        ("api", "--include"),
        ("api", "--paginate"),
        ("release", "download"),
    ]


@pytest.mark.parametrize(
    ("tag", "asset_pattern"),
    [
        ("app-latest", "Meiva_{}_universal.dmg"),
        ("app-latest", "Meiva_{}.app.tar.gz"),
        ("android-latest", "meiva-app-{}.apk"),
    ],
)
@pytest.mark.parametrize("recovery_patch", [162, 163])
def test_feed_recovers_after_manifest_replacement_exhausts_retries(
    tmp_path: Path, tag: str, asset_pattern: str, recovery_patch: int
) -> None:
    manifest_name = "latest.json" if tag == "app-latest" else "android-latest.json"

    def manifest_bytes(patch: int) -> bytes:
        return (
            f'{{"version":"1.0.{patch}"}}'
            if tag == "app-latest"
            else f'{{"versionName":"1.0.{patch}","versionCode":{1000000 + patch}}}'
        ).encode()

    previous_name = asset_pattern.format("1.0.161")
    staged_name = asset_pattern.format("1.0.162")
    release = _Release(
        assets={previous_name: b"previous binary", manifest_name: manifest_bytes(161)},
        manifest_failures=3,
    )

    def publish(patch: int) -> None:
        version = f"1.0.{patch}"
        binary = tmp_path / asset_pattern.format(version)
        manifest = tmp_path / manifest_name
        _ = binary.write_bytes(version.encode())
        _ = manifest.write_bytes(manifest_bytes(patch))

        def assert_advances(previous: Path) -> None:
            if tag == "app-latest":
                assert_version_advances(current=previous, version=version)
            else:
                assert_version_code_advances(current=previous, version_code=1000000 + patch)

        publish_feed(
            release.gh,
            tag=tag,
            version=version,
            files=(binary, manifest),
            title=version,
            notes="",
            target="a" * 40,
            manifest_attempts=3,
            wait=lambda _: None,
            assert_advances=assert_advances,
        )

    with pytest.raises(subprocess.CalledProcessError):
        publish(162)
    staged = {previous_name: b"previous binary", staged_name: b"1.0.162"}
    assert release.assets == staged

    release.calls.clear()
    with pytest.raises(ValueError, match=r"1\.0\.162"):
        publish(2)
    assert release.assets == staged
    assert all(call[0] == "api" for call in release.calls)

    publish(recovery_patch)
    version = f"1.0.{recovery_patch}"
    assert release.assets == {
        asset_pattern.format(version): version.encode(),
        manifest_name: manifest_bytes(recovery_patch),
    }

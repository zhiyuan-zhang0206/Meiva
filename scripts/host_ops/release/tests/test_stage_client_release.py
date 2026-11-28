from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.host_ops.release.client_release_baseline import fetch_baseline
from scripts.host_ops.release.github_releases import GitHubCommandError
from scripts.host_ops.release.stage_client_release import StageOptions, stage_client_release

_LATEST = "client-v1.0.203"
_COMMIT = "a" * 40
_PRIOR_COMMIT = "b" * 40
_BUILT_AT = "2026-11-28T10:00:00Z"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def app_signing(tmp_path: Path) -> tuple[Path, bytes]:
    key = Ed25519PrivateKey.generate()
    key_id = b"test-key"
    config = tmp_path / "tauri.conf.json"
    pubkey = b"untrusted comment: test key\n" + base64.b64encode(
        b"Ed" + key_id + key.public_key().public_bytes_raw()
    )
    _ = config.write_text(
        json.dumps({"plugins": {"updater": {"pubkey": base64.b64encode(pubkey).decode()}}})
    )
    comment = b"archive"
    signature = key.sign(hashlib.blake2b(b"archive").digest())
    lines = (
        b"untrusted comment: stage fixture",
        base64.b64encode(b"ED" + key_id + signature),
        b"trusted comment: " + comment,
        base64.b64encode(key.sign(signature + comment)),
    )
    return config, base64.b64encode(b"\n".join(lines) + b"\n")


def _app(version: str, signature: bytes) -> dict[str, bytes]:
    return {
        f"Meiva_{version}_universal.dmg": b"disk image",
        f"Meiva_{version}.app.tar.gz": b"archive",
        f"Meiva_{version}.app.tar.gz.sig": signature,
    }


def _android(version: str) -> dict[str, bytes]:
    return {f"meiva-app-{version}.apk": f"apk {version}".encode()}


def _prior(signature: bytes, mismatched_target: str | None = None) -> dict[str, bytes]:
    app = _app("1.0.201", signature)
    platforms = {
        target: {"signature": "different" if target == mismatched_target else signature.decode()}
        for target in ("darwin-aarch64", "darwin-x86_64")
    }
    android = _android("1.0.203")
    return (
        app
        | android
        | {
            "latest.json": json.dumps(
                {"version": "1.0.201", "commit": _PRIOR_COMMIT, "platforms": platforms}
            ).encode(),
            "android-latest.json": json.dumps(
                {
                    "versionName": "1.0.203",
                    "versionCode": 1_000_203,
                    "commit": _PRIOR_COMMIT,
                    "builtAt": _BUILT_AT,
                    "sha256": _sha256(android["meiva-app-1.0.203.apk"]),
                },
            ).encode(),
        }
    )


class _GitHub:
    def __init__(self, releases: dict[str, dict[str, bytes]]) -> None:
        self.releases = releases
        self.lookups: list[str] = []

    def __call__(self, *args: str) -> str:
        if args[:2] == ("api", "--include"):
            selector = args[2].split("/releases/", maxsplit=1)[1]
            self.lookups.append(selector)
            tag = _LATEST if selector == "latest" else selector.removeprefix("tags/")
            if tag not in self.releases:
                raise GitHubCommandError(1, args, "HTTP/2.0 404 Not Found\n")
            assets = self.releases[tag]
            return "HTTP/2.0 200 OK\n\n" + "\n".join(
                [tag, *(f"{name}\tsha256:{_sha256(data)}" for name, data in assets.items())],
            )
        if args[:2] == ("release", "download"):
            _, _, tag, _, name, _, output = args
            _ = Path(output).write_bytes(self.releases[tag][name])
            return ""
        raise AssertionError(args)


@pytest.mark.parametrize("rebuild", ["app", "android", "both"])
def test_stage_carries_the_other_platform_with_its_own_identity(
    tmp_path: Path, rebuild: str, app_signing: tuple[Path, bytes]
) -> None:
    config, signature = app_signing
    new_app = rebuild in {"app", "both"}
    new_android = rebuild in {"android", "both"}
    app_version = "1.0.204" if new_app else "1.0.201"
    android_version = "1.0.204" if new_android else "1.0.203"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    if new_app:
        for name, data in _app(app_version, signature).items():
            _ = (artifacts / name).write_bytes(data)
    if new_android:
        for name, data in _android(android_version).items():
            _ = (artifacts / name).write_bytes(data)
    gh = _GitHub({_LATEST: _prior(signature)})
    output = tmp_path / "output"

    stage_client_release(
        gh,
        StageOptions(
            app_version=app_version if new_app else "",
            android_version=android_version if new_android else "",
            android_code=1_000_204 if new_android else 0,
            android_built_at="2026-11-28T11:00:00Z" if new_android else "",
            commit=_COMMIT,
            repo="owner/Meiva",
            artifacts=artifacts,
            workspace=tmp_path,
            github_output=output,
        ),
        tauri_config=config,
    )

    assert gh.lookups == ["latest"]
    assert output.read_text() == f"version=1.0.204\nlatest={_LATEST}\n"
    _assert_staged(tmp_path / "assets", app_version, android_version, signature)


def _assert_staged(assets: Path, app_version: str, android_version: str, signature: bytes) -> None:
    new_app = app_version == "1.0.204"
    new_android = android_version == "1.0.204"
    expected = _app(app_version, signature) | _android(android_version)
    assert {p.name for p in assets.iterdir()} == expected.keys() | {
        "latest.json",
        "android-latest.json",
    }
    assert all((assets / name).read_bytes() == data for name, data in expected.items())
    prefix = "https://github.com/owner/Meiva/releases/download/client-v1.0.204/"
    mac = (assets / "latest.json").read_text()
    assert prefix + f"Meiva_{app_version}.app.tar.gz" in mac
    assert f'"commit": "{_COMMIT if new_app else _PRIOR_COMMIT}"' in mac
    android = (assets / "android-latest.json").read_text()
    assert prefix + f"meiva-app-{android_version}.apk" in android
    assert f'"commit": "{_COMMIT if new_android else _PRIOR_COMMIT}"' in android
    assert f'"versionCode": {1_000_204 if new_android else 1_000_203}' in android


@pytest.mark.parametrize("target", ["darwin-aarch64", "darwin-x86_64"])
def test_stage_rejects_a_carried_signature_that_differs_from_baseline(
    tmp_path: Path, app_signing: tuple[Path, bytes], target: str
) -> None:
    config, signature = app_signing
    output = tmp_path / "output"
    with pytest.raises(ValueError, match=f"does not match the prior macOS manifest for {target}"):
        stage_client_release(
            _GitHub({_LATEST: _prior(signature, mismatched_target=target)}),
            StageOptions(
                app_version="",
                android_version="1.0.204",
                android_code=1_000_204,
                android_built_at=_BUILT_AT,
                commit=_COMMIT,
                repo="owner/Meiva",
                artifacts=tmp_path,
                workspace=tmp_path,
                github_output=output,
            ),
            tauri_config=config,
        )
    assert not output.exists()


def test_baseline_falls_back_only_for_missing_manifest_and_tolerates_retirement(
    tmp_path: Path,
) -> None:
    gh = _GitHub(
        {
            _LATEST: {"latest.json": b"from Latest"},
            "android-latest": {"android-latest.json": b"from legacy"},
        }
    )
    current = fetch_baseline(gh, tmp_path / "migration")
    assert gh.lookups == ["latest", "tags/android-latest"]
    assert current.app is not None and current.app.tag == _LATEST
    assert current.android is not None and current.android.tag == "android-latest"
    assert (tmp_path / "migration" / "latest.json").read_bytes() == b"from Latest"
    assert (tmp_path / "migration" / "android-latest.json").read_bytes() == b"from legacy"

    gh = _GitHub({_LATEST: {}})
    current = fetch_baseline(gh, tmp_path / "no-feeds")
    assert current.latest_tag == _LATEST
    assert current.app is None and current.android is None
    assert not tuple((tmp_path / "no-feeds").iterdir())

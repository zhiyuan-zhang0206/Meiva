from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.codegen import build_app_update_manifest as manifest_builder


def _artifact(directory: Path, name: str, signature: str | None = None) -> None:
    (directory / name).write_bytes(b"bundle")
    if signature is not None:
        (directory / f"{name}.sig").write_text(signature)


@pytest.fixture
def signed_bundle(tmp_path: Path) -> tuple[Path, str]:
    key = Ed25519PrivateKey.generate()
    key_id = b"test-key"
    comment = b"timestamp:1795860559\tfile:Meiva.app.tar.gz"
    signature = key.sign(hashlib.blake2b(b"bundle").digest())
    lines = [
        b"untrusted comment: signature from tauri secret key",
        base64.b64encode(b"ED" + key_id + signature),
        b"trusted comment: " + comment,
        base64.b64encode(key.sign(signature + comment)),
    ]
    encoded = base64.b64encode(b"\n".join(lines) + b"\n").decode()
    pubkey = (
        b"untrusted comment: test key\n"
        + base64.b64encode(b"Ed" + key_id + key.public_key().public_bytes_raw())
        + b"\n"
    )
    config = tmp_path / "tauri.conf.json"
    _ = config.write_text(
        json.dumps(
            {
                "plugins": {
                    "updater": {
                        "pubkey": base64.b64encode(pubkey).decode(),
                    }
                }
            }
        )
    )
    return config, encoded


def test_builds_tauri_static_manifest_for_universal_macos(
    tmp_path: Path, signed_bundle: tuple[Path, str]
) -> None:
    config, signature = signed_bundle
    _artifact(tmp_path, "Meiva.app.tar.gz", signature + "\n")
    _artifact(tmp_path, "Meiva_0.4.1_aarch64.dmg")
    _artifact(tmp_path, "Meiva_0.4.1.apk")

    manifest = manifest_builder.build_manifest(
        tag="app-v0.4.1",
        tauri_config=config,
        artifacts=tmp_path,
        repo="owner/Meiva",
    )

    assert manifest["version"] == "0.4.1"
    assert set(manifest["platforms"]) == {
        "darwin-aarch64",
        "darwin-x86_64",
    }
    assert manifest["platforms"]["darwin-aarch64"] == {
        "url": "https://github.com/owner/Meiva/releases/download/app-v0.4.1/Meiva.app.tar.gz",
        "signature": signature,
    }


def test_unsigned_build_still_emits_an_honest_empty_feed(tmp_path: Path) -> None:
    _artifact(tmp_path, "Meiva.app.tar.gz")

    manifest = manifest_builder.build_manifest(
        tag="app-v0.4.1",
        artifacts=tmp_path,
        repo="owner/Meiva",
    )

    assert manifest == {"version": "0.4.1", "platforms": {}}


@pytest.mark.parametrize("tag", ["v0.4.1", "app-v1", "app-v1.2.x", "app-v1.2.3.4"])
def test_rejects_tags_outside_the_app_semver_namespace(tmp_path: Path, tag: str) -> None:
    with pytest.raises(ValueError):
        manifest_builder.build_manifest(tag=tag, artifacts=tmp_path, repo="owner/Meiva")


def test_rejects_ambiguous_archives(tmp_path: Path) -> None:
    _artifact(tmp_path, "Meiva.app.tar.gz", "first")
    _artifact(tmp_path, "Meiva-copy.app.tar.gz", "second")

    with pytest.raises(ValueError, match="multiple macOS updater archives"):
        manifest_builder.build_manifest(
            tag="app-v0.4.1",
            artifacts=tmp_path,
            repo="owner/Meiva",
        )


def test_version_name_increments_patch_by_run_number() -> None:
    assert manifest_builder.build_version_name("1.0.0", 1) == "1.0.1"
    assert manifest_builder.build_version_name("1.0.0", 42) == "1.0.42"
    with pytest.raises(ValueError, match="run number must be positive"):
        manifest_builder.build_version_name("1.0.0", 0)


def test_builds_client_release_manifest_for_a_carried_macos_build(
    tmp_path: Path, signed_bundle: tuple[Path, str]
) -> None:
    config, signature = signed_bundle
    _artifact(tmp_path, "Meiva_1.0.15.app.tar.gz", signature)
    commit = "a" * 40

    manifest = manifest_builder.build_manifest(
        tauri_config=config,
        tag="client-v1.0.16",
        version="1.0.15",
        commit=commit,
        artifacts=tmp_path,
        repo="owner/Meiva",
    )

    assert manifest == {
        "version": "1.0.15",
        "commit": commit,
        "platforms": {
            "darwin-aarch64": {
                "url": "https://github.com/owner/Meiva/releases/download/client-v1.0.16/Meiva_1.0.15.app.tar.gz",
                "signature": signature,
            },
            "darwin-x86_64": {
                "url": "https://github.com/owner/Meiva/releases/download/client-v1.0.16/Meiva_1.0.15.app.tar.gz",
                "signature": signature,
            },
        },
    }


def test_assert_version_advances_guards_latest(tmp_path: Path) -> None:
    current = tmp_path / "latest.json"
    current.write_text('{"version": "1.0.10", "platforms": {}}\n', encoding="utf-8")

    manifest_builder.assert_version_advances(current=current, version="1.0.11")
    with pytest.raises(ValueError, match=r"already advertises 1\.0\.10"):
        manifest_builder.assert_version_advances(current=current, version="1.0.10")
    with pytest.raises(ValueError, match=r"already advertises 1\.0\.10"):
        manifest_builder.assert_version_advances(current=current, version="1.0.9")


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("garbage", "invalid minisign signature"),
        ("key id", "key ID"),
        ("archive", "signature verification failed"),
        ("trusted comment", "signature verification failed"),
    ],
)
def test_rejects_invalid_updater_signatures(
    tmp_path: Path, signed_bundle: tuple[Path, str], tamper: str, message: str
) -> None:
    config, signature = signed_bundle
    lines = base64.b64decode(signature).splitlines()
    if tamper == "key id":
        packet = base64.b64decode(lines[1])
        lines[1] = base64.b64encode(packet[:2] + b"fake-key" + packet[10:])
    if tamper == "trusted comment":
        lines[2] += b" modified"
    signature = base64.b64encode(b"\n".join(lines) + b"\n").decode()
    _artifact(tmp_path, "Meiva.app.tar.gz", signature if tamper != "garbage" else "non-empty")
    if tamper == "archive":
        _ = (tmp_path / "Meiva.app.tar.gz").write_bytes(b"tampered")

    with pytest.raises(ValueError, match=message):
        _ = manifest_builder.build_manifest(
            tag="app-v0.4.1", artifacts=tmp_path, repo="owner/Meiva", tauri_config=config
        )

import plistlib
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-app.yml"
_ANDROID_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-android.yml"
_MACOS_INFO_PLIST = _REPO_ROOT / "ui" / "app" / "src-tauri" / "Info.plist"


def _workflow() -> str:
    return _WORKFLOW.read_text()


def _android_workflow() -> str:
    return _ANDROID_WORKFLOW.read_text()


def test_macos_bundle_allows_cleartext_in_web_content_only() -> None:
    with _MACOS_INFO_PLIST.open("rb") as source:
        plist = cast("dict[str, object]", plistlib.load(source, fmt=plistlib.FMT_XML))
    ats = plist["NSAppTransportSecurity"]
    assert isinstance(ats, dict)
    assert ats["NSAllowsArbitraryLoadsInWebContent"] is True
    assert "NSAllowsArbitraryLoads" not in ats


def test_tauri_cli_is_exactly_pinned() -> None:
    text = _workflow()
    assert "TAURI_CLI_VERSION: '2.11.4'" in text
    assert "TAURI_CLI_VERSION: '^" not in text


_APPLE_SIGNING = {
    "APPLE_CERTIFICATE": "certificate",
    "APPLE_CERTIFICATE_PASSWORD": "certificate-password",
    "APPLE_SIGNING_IDENTITY": "identity",
}
_APPLE_NOTARIZATION = {
    "APPLE_ID": "id",
    "APPLE_PASSWORD": "password",
    "APPLE_TEAM_ID": "team",
}
_UPDATER_SIGNING = {"TAURI_SIGNING_PRIVATE_KEY": "key"}
_APPLE_SECRETS = _APPLE_SIGNING | _APPLE_NOTARIZATION


@pytest.mark.parametrize(
    ("event", "publish", "secrets", "error"),
    [
        ("workflow_call", "true", _UPDATER_SIGNING, ""),
        ("workflow_dispatch", "true", _UPDATER_SIGNING, ""),
        ("workflow_dispatch", "false", {}, ""),
        ("workflow_call", "true", {}, "Published macOS releases require updater signing"),
        ("workflow_dispatch", "true", {}, "Published macOS releases require updater signing"),
        ("push", "true", _APPLE_SECRETS, "Published macOS releases require updater signing"),
        ("push", "true", _UPDATER_SIGNING, "Release tags require Apple signing and notarization"),
        (
            "push",
            "true",
            _UPDATER_SIGNING | _APPLE_SIGNING,
            "Release tags require Apple signing and notarization",
        ),
        ("push", "true", _UPDATER_SIGNING | _APPLE_SECRETS, ""),
        ("workflow_call", "true", _UPDATER_SIGNING | _APPLE_SECRETS, ""),
        (
            "workflow_call",
            "true",
            _UPDATER_SIGNING | {"APPLE_CERTIFICATE": "certificate"},
            "Apple code-signing secrets are only partially configured",
        ),
        (
            "workflow_dispatch",
            "true",
            _UPDATER_SIGNING | {"APPLE_ID": "id"},
            "Apple notarization secrets are only partially configured",
        ),
    ],
)
def test_macos_release_signing_policy(
    event: str, publish: str, secrets: dict[str, str], error: str
) -> None:
    desktop = yaml.safe_load(_workflow())["jobs"]["desktop"]
    assert desktop["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert desktop["env"]["PUBLISH_RELEASE"] == "${{ needs.prepare.outputs.publish }}"
    [validate] = [
        step
        for step in desktop["steps"]
        if step.get("name") == "Validate optional signing secret groups"
    ]
    result = subprocess.run(  # noqa: S603 -- execute the checked-in signing validation step
        ["bash", "-eo", "pipefail", "-c", validate["run"]],
        env={"EVENT_NAME": event, "PUBLISH_RELEASE": publish} | secrets,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stderr.strip()) == (int(bool(error)), error)


def test_android_release_workflow_is_separate_and_fails_closed() -> None:
    text = _android_workflow()
    assert "android-v[0-9]+.[0-9]+.[0-9]+" in text
    assert "Release tags require Android signing" in text


def test_android_build_injects_the_run_number_as_version_code() -> None:
    text = _android_workflow()
    assert "build_android_release.py version-code" in text
    assert "build_number: ${{ steps.build.outputs.build_number }}" in text
    assert "tauri-config" in text
    assert '--version-name "$APP_VERSION" --version-code "$BUILD_NUMBER"' in text
    assert "versionCode='$BUILD_NUMBER'" in text


def test_android_latest_publishes_the_manifest_with_the_apk() -> None:
    text = _android_workflow()
    assert "build_android_release.py manifest" in text
    assert "--out release-assets/android-latest.json" in text
    assert '"$apk" release-assets/android-latest.json' in text
    assert "scripts.host_ops.release.publish_feed --tag android-latest" in text


def test_android_manual_dispatch_builds_without_publishing_by_default() -> None:
    text = _android_workflow()
    assert "dry_run:" in text
    assert "default: true" in text
    assert "toJSON(inputs.dry_run) == 'false'" in text
    assert "if: needs.prepare.outputs.publish == 'true'" in text


def test_android_publish_refuses_to_roll_the_channel_back() -> None:
    text = _android_workflow()
    assert "publish_feed --tag android-latest" in text
    assert '--version "$APP_VERSION" --version-code "$BUILD_NUMBER" --target "$TARGET_SHA"' in text


def test_manual_dispatch_keeps_the_explicit_unsigned_build_path() -> None:
    text = _workflow()
    assert "workflow_dispatch:" in text
    assert "EXTRA+=(--no-sign)" in text


def test_merging_to_main_does_not_trigger_client_release_builds() -> None:
    for text in (_workflow(), _android_workflow()):
        assert "branches:" not in text
        assert "workflow_call:" in text
        assert "build_app_update_manifest.py" in text
        assert "version-name" in text


def test_desktop_publish_guards_app_latest_and_stamps_commit() -> None:
    text = _workflow()
    assert 'archive="release-assets/Meiva_${APP_VERSION}.app.tar.gz"' in text
    assert 'cp artifacts/Meiva.app.tar.gz.sig "$archive.sig"' in text
    _, publish = text.split("      - name: Publish the macOS release assets", maxsplit=1)
    step, _ = publish.split("        run: |", maxsplit=1)
    assert "if:" not in step
    feed, tag = publish.split('if [ "$EVENT_NAME" = "push" ]; then', maxsplit=1)
    assert "scripts.host_ops.release.publish_feed --tag app-latest" in feed
    assert '--version "$APP_VERSION" --target "$TARGET_SHA"' in feed
    assert '--tag "$REF_NAME" --version "$APP_VERSION" --commit "$TARGET_SHA"' in tag

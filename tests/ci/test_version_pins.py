# meiva:note
# Cargo and npm cannot read pyproject.toml, so the app crate and the frontend
# package carry pinned copies; their lockfiles, the generated OpenAPI document
# and the Tauri bundle config must stay equal to that one product version.
# meiva:link docs/conventions/engineering/host-versioning.md
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def _toml(relative: str) -> dict[str, Any]:
    return tomllib.loads((_REPO_ROOT / relative).read_text(encoding="utf-8"))


def _json(relative: str) -> dict[str, Any]:
    return json.loads((_REPO_ROOT / relative).read_text(encoding="utf-8"))


def _text(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


def _product_version() -> str:
    version = _toml("pyproject.toml")["project"]["version"]
    assert isinstance(version, str)
    return version


def _locked_package(relative: str, name: str) -> str:
    packages = _toml(relative).get("package", [])
    matches = [package for package in packages if package.get("name") == name]
    assert len(matches) == 1, f"{relative} must list exactly one {name!r} package"
    return matches[0]["version"]


def test_pyproject_is_the_one_hand_maintained_version() -> None:
    version = _product_version()
    assert _SEMVER.fullmatch(version), f"version {version!r} is not X.Y.Z"


def test_app_crate_matches_the_product_version() -> None:
    assert _toml("ui/app/src-tauri/Cargo.toml")["package"]["version"] == _product_version()


def test_app_crate_lock_matches_its_manifest() -> None:
    assert _locked_package("ui/app/src-tauri/Cargo.lock", "meiva-app") == _product_version()


def test_frontend_package_matches_the_product_version() -> None:
    assert _json("ui/web/package.json")["version"] == _product_version()


def test_frontend_lock_root_matches_its_package() -> None:
    lock = _json("ui/web/package-lock.json")
    assert lock["version"] == _product_version()
    assert lock["packages"][""]["version"] == _product_version()


def test_python_lock_matches_the_project_metadata() -> None:
    assert _locked_package("uv.lock", "meiva") == _product_version()


def test_tauri_config_derives_the_app_version_from_the_crate() -> None:
    config = _json("ui/app/src-tauri/tauri.conf.json")
    assert "version" not in config, "Tauri must keep deriving its version from Cargo.toml"
    android = config.get("bundle", {}).get("android", {})
    assert "versionCode" not in android, "the release overlay owns versionCode"


def test_generated_openapi_document_carries_the_product_version() -> None:
    assert _json("ui/web/openapi.json")["info"]["version"] == _product_version()


def test_python_package_declares_a_stable_release_state() -> None:
    classifiers = _toml("pyproject.toml")["project"]["classifiers"]
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert not [item for item in classifiers if "Alpha" in item or "Beta" in item]


def test_changelog_names_the_current_version() -> None:
    text = _text("CHANGELOG.md")
    assert f"Meiva is at {_product_version()};" in text
    assert "pre-1.0" not in text


def test_security_policy_names_the_current_version() -> None:
    assert f"Meiva {_product_version()} ships from `main`" in _text("SECURITY.md")


def test_app_doc_names_the_crate_version() -> None:
    text = _text("ui/app/docs/app.meiva.okf.md")
    assert f"`Cargo.toml` is version `{_product_version()}`" in text
    assert "mirrors the product version in" in text
    assert "tests/ci/test_version_pins.py" in text


def test_release_workflows_take_the_release_version_from_the_crate() -> None:
    for relative in (
        ".github/workflows/release-app.yml",
        ".github/workflows/release-android.yml",
    ):
        text = _text(relative)
        assert 'pathlib.Path("ui/app/src-tauri/Cargo.toml")' in text
        assert "cargo_version" in text


def test_android_version_code_rule_keeps_its_run_number_base() -> None:
    text = _text("scripts/codegen/build_android_release.py")
    assert "BUILD_NUMBER_BASE = 1_000_000" in text
    assert "return _checked_version_code(BUILD_NUMBER_BASE + run_number)" in text

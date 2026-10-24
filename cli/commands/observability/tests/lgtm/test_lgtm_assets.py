# meiva:note
# The checkout's pinned native LGTM release assets.

from __future__ import annotations

import yaml

from base.telemetry.lgtm_local import NATIVE_SERVICES
from cli.commands.observability import lgtm_native
from cli.commands.observability.tests.observability_inputs import REPO_ROOT


def test_versions_file_has_the_pinned_release_assets() -> None:
    versions_path = REPO_ROOT / "deploy/lgtm/native/versions.yml"
    versions = yaml.safe_load(versions_path.read_text(encoding="utf-8"))

    darwin_versions = {
        name: {
            "version": spec["version"],
            "assets": {"darwin-arm64": spec["assets"]["darwin-arm64"]},
        }
        for name, spec in versions.items()
    }
    assert darwin_versions == {
        "loki": {
            "version": "3.7.6",
            "assets": {
                "darwin-arm64": {
                    "url": "https://github.com/grafana/loki/releases/download/v3.7.6/loki-darwin-arm64.zip",
                    "sha256": "c189a879f040c823b815051ccbc145a23f6799cb531d619a06c0e8cce7076826",
                }
            },
        },
        "prometheus": {
            "version": "3.13.2",
            "assets": {
                "darwin-arm64": {
                    "url": "https://github.com/prometheus/prometheus/releases/download/v3.13.2/prometheus-3.13.2.darwin-arm64.tar.gz",
                    "sha256": "f68ca4f1dbedd6366bbfdd8ac5d2c0b7ba1f273474acc8d38eb33202fbeec7a4",
                }
            },
        },
        "grafana": {
            "version": "13.2.3",
            "assets": {
                "darwin-arm64": {
                    "url": "https://dl.grafana.com/oss/release/grafana-13.2.3.darwin-arm64.tar.gz",
                    "sha256": "248a51bcfacdb1ec642006cee46b4de44a34bcf41ddea88d96a4605e2e9d808c",
                }
            },
        },
    }


def test_every_backend_has_an_asset_for_each_supported_platform() -> None:
    for tag in ("darwin_arm64", "linux_amd64"):
        pins = lgtm_native.native_versions(REPO_ROOT, tag=tag)

        assert set(pins) == set(NATIVE_SERVICES)
        for asset in pins.values():
            assert asset["url"].startswith("https://")
            assert len(asset["sha256"]) == 64


def test_the_executable_members_name_the_platform_they_were_pinned_for() -> None:
    linux = lgtm_native.native_versions(REPO_ROOT, tag="linux_amd64")

    assert linux["loki"]["member"] == "loki-linux-amd64"
    assert linux["prometheus"]["member"].endswith(".linux-amd64/prometheus")
    assert linux["grafana"]["member"] == ""

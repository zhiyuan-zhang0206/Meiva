from __future__ import annotations

import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded


def test_catalog_uses_exact_home_freezes_images_and_recovers_after_bad_config(
    tmp_path: Path,
) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-m",
            "meiva_builtins.plugins.lm_typesafe.tests.catalog_child",
            str(tmp_path),
        ],
        timeout=30,
        capture_output=True,
        text=True,
        env={
            "MEIVA_HOME": str(tmp_path / "ambient"),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_MACHINE_SERVE_GATEWAY": "true",
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "false",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run_bootstrap_child(tmp_path: Path, mode: str, delivery: dict[str, str]) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-m",
            "meiva_builtins.plugins.lm_typesafe.tests.bootstrap_child",
            str(tmp_path),
            mode,
        ],
        timeout=30,
        capture_output=True,
        text=True,
        env={"MEIVA_HOME": str(tmp_path), **delivery},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bare_scratch_home_needs_no_projection(tmp_path: Path) -> None:
    _run_bootstrap_child(tmp_path, "scratch", {"MEIVA_CONFIG_FETCH": "skip"})


@pytest.mark.parametrize("skip", [False, True])
def test_enrolled_runner_rejects_missing_projection(tmp_path: Path, skip: bool) -> None:
    delivery = {
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "MEIVA_GATEWAY_URL": "http://127.0.0.1:1",
    }
    (tmp_path / ".env").write_text(
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\nMEIVA_GATEWAY_URL=http://127.0.0.1:1\n"
    )
    if skip:
        delivery["MEIVA_CONFIG_FETCH"] = "skip"
    _run_bootstrap_child(tmp_path, "missing", delivery)


@pytest.mark.parametrize("mode", ["valid", "malformed", "bad-fields", "bad-values"])
def test_runner_consumes_real_schema_owned_projection(tmp_path: Path, mode: str) -> None:
    import json

    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
    from base.packages.plugin_config_images import config_image_path
    from base.packages.plugins.config_registration import plugin_bootstrap_config
    from meiva_builtins.plugins.lm_typesafe.tests.test_local_decision import (
        MODEL,
        config_values,
    )

    gateway_home = tmp_path / "gateway"
    values = config_values()
    values["local_models"][MODEL]["wire_model"] = "gateway-model"
    image = config_image_path("lm_typesafe", home=gateway_home)
    image.parent.mkdir(parents=True)
    image.write_text(json.dumps(values))
    packet = plugin_bootstrap_config(home=gateway_home)
    if mode == "malformed":
        packet = "bad json"
    elif mode == "bad-fields":
        contents = json.loads(packet)
        contents["lm_typesafe"].pop("local_models")
        packet = json.dumps(contents)
    elif mode == "bad-values":
        contents = json.loads(packet)
        contents["lm_typesafe"]["local_endpoint"] = "invalid"
        packet = json.dumps(contents)
    runner_home = tmp_path / "runner"
    local = config_values()
    local["local_models"][MODEL]["wire_model"] = "wrong-local-model"
    runner_image = config_image_path("lm_typesafe", home=runner_home)
    runner_image.parent.mkdir(parents=True)
    runner_image.write_text(json.dumps(local))
    _run_bootstrap_child(
        runner_home,
        mode,
        {
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
            "MEIVA_GATEWAY_URL": "http://127.0.0.1:1",
            PLUGIN_CLUSTER_CONFIG_ENV: packet,
        },
    )

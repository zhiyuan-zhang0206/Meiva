import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from base.lm.factory import process_provider_keys, validate_model_launch
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.lm.providers.provider_plugin_support import (
    provider_plugin as provider_plugin,
)
from base.paths import meiva_home


@pytest.mark.parametrize("key_state", ["present", "absent"])
def test_provider_keys_only_read_dotenv_when_one_is_absent(tmp_path: Path, key_state: str) -> None:
    child = Path(__file__).with_name("_provider_key_child.py")
    live = dict.fromkeys(build_model_catalog(home=meiva_home()).key_envs, "test-provider-key")
    if key_state == "absent":
        live.pop("DEEPSEEK_API_KEY")
    result = run_bounded(
        [sys.executable, "-I", str(child), str(tmp_path), key_state],
        timeout=30,
        capture_output=True,
        text=True,
        env={
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
            **live,
        },
    )
    assert result.returncode == 0, result.stderr


def test_plugin_model_validation_and_key_check(
    unit_home: Path, provider_plugin: Callable[..., None]
) -> None:
    provider_plugin()
    catalog = build_model_catalog(home=unit_home)
    assert "TESTP_API_KEY" in catalog.key_envs
    assert "TESTP_API_KEY" not in os.environ
    (unit_home / ".env").write_text("")
    with pytest.raises(ValueError, match="TESTP_API_KEY"):
        validate_model_launch(
            model="testp-1",
            catalog=catalog,
            llm_override="",
            keys=process_provider_keys(catalog, environ=os.environ),
        )

    (unit_home / ".env").write_text("TESTP_API_KEY=sk-test\n")
    assert (
        validate_model_launch(
            model="testp-1",
            catalog=catalog,
            llm_override="",
            keys=process_provider_keys(catalog, environ=os.environ),
        )
        == "testp-1"
    )

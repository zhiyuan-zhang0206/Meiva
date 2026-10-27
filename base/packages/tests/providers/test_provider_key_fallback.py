import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from base.packages.plugin_providers import build_model_catalog
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

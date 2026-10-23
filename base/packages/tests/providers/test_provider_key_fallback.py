import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded


@pytest.mark.parametrize("key_state", ["present", "absent"])
def test_provider_key_only_reads_dotenv_when_absent(tmp_path: Path, key_state: str) -> None:
    child = Path(__file__).with_name("_provider_key_child.py")
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
            "DEEPSEEK_API_KEY": "test-provider-key" if key_state == "present" else "",
        },
    )
    assert result.returncode == 0, result.stderr

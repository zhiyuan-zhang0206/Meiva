import sys
from pathlib import Path

import pytest

from services.wake.schedule_manager import engine, runner


@pytest.mark.parametrize("script", ["pass", "raise RuntimeError('payload failed')"])
def test_payload_restores_runner_argv_even_when_script_fails(tmp_path: Path, script: str) -> None:
    payload = tmp_path / "run.py"
    payload.write_text("import sys\nassert sys.argv == [__file__]\n" + script)
    original = sys.argv
    original_values = original[:]
    if script == "pass":
        engine.run_single_round_payload(payload)
    else:
        with pytest.raises(RuntimeError, match="payload failed"):
            engine.run_single_round_payload(payload)
    assert sys.argv is original
    assert sys.argv == original_values


def test_main_refuses_on_foreign_checkout(unit_home: Path) -> None:
    (unit_home / "source").mkdir()
    with pytest.raises(SystemExit) as caught:
        runner.main(["1"])
    assert caught.value.code == 3

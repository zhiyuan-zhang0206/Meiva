from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from base.host.proc import run_bounded

_CHILD = Path(__file__).with_name("child_repair_billing.py")


# meiva:note
# Both repair calls of the child, with real tracing armed in a fresh process (Traceloop owns one
# tracer provider per process, so the billing spans are read where they are ended).
@pytest.fixture(scope="module")
def repairs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    home = tmp_path_factory.mktemp("repair-billing-home")
    proc = run_bounded(
        [sys.executable, "-I", str(_CHILD)],
        timeout=120,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_successful_syntax_repair_call_emits_chat_billing(repairs: dict[str, Any]) -> None:
    assert repairs["answers"]["repaired"] == "fixed = True"
    [span] = repairs["answers"]["spans"]
    assert span["name"] == "meiva.billing.call"
    attributes = span["attributes"]
    assert attributes["meiva.billing.model"] == "deepseek-flash"
    assert attributes["meiva.billing.usage_kind"] == "chat"
    assert attributes["meiva.billing.vendor"] == "deepseek"
    assert (attributes["meiva.billing.tokens_in"], attributes["meiva.billing.tokens_out"]) == (
        10,
        2,
    )


def test_unavailable_syntax_repair_model_emits_no_billing(repairs: dict[str, Any]) -> None:
    assert repairs["unavailable"] == {"repaired": None, "spans": []}

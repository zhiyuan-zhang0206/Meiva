from __future__ import annotations

import json
import sys
import tempfile
from functools import cache
from pathlib import Path

from pydantic import TypeAdapter

from base.host.proc import run_bounded


# meiva:link tests/docs/test-fixtures/catalog-boot.md
@cache
def roster_models() -> tuple[str, ...]:
    with tempfile.TemporaryDirectory(prefix="meiva-model-roster-") as home:
        result = run_bounded(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_model_config_cases.py")),
                "catalog_roster",
            ],
            timeout=60,
            capture_output=True,
            text=True,
            env={"MEIVA_HOME": home, "MEIVA_CONFIG_FETCH": "skip"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    return TypeAdapter(tuple[str, ...]).validate_json(result.stdout)


def provider_keys() -> dict[str, str]:
    return {
        "ANTHROPIC_API_KEY": "sk-test",
        "DEEPSEEK_API_KEY": "sk-test",
        "GEMINI_API_KEY": "sk-test",
        "OPENAI_API_KEY": "sk-test",
        "GLM_API_KEY": "sk-test",
        "DASHSCOPE_API_KEY": "sk-test",
        "MOONSHOT_API_KEY": "sk-test",
        "MIMO_API_KEY": "sk-test",
    }


def run_case(home: Path, case: str, keys: dict[str, str], *arguments: str) -> None:
    legacy = case.endswith(("missing_mimo_key_raises", "missing_kimi_key_raises"))
    initial = (
        {"MIMO_API_KEY": "legacy-settings-key", "MOONSHOT_API_KEY": "legacy-settings-key"}
        if legacy
        else keys
        if case.startswith("thinking_")
        else {}
    )
    _ = (home / ".env").write_text(
        "".join(f"{name}={value}\n" for (name, value) in initial.items())
    )
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_model_config_cases.py")),
            case,
            json.dumps(keys),
            *arguments,
        ],
        timeout=60,
        capture_output=True,
        text=True,
        env={
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent"
            if legacy or case.startswith("thinking_")
            else "gateway",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def run_thinking_disabled_case(home: Path, model: str) -> None:
    run_case(
        home, "thinking_roster_model_constructs_with_thinking_disabled", provider_keys(), model
    )

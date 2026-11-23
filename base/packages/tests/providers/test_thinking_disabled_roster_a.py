from __future__ import annotations

from pathlib import Path

import pytest

from base.packages.tests.providers.model_config_process import (
    roster_models,
    run_case,
    run_thinking_disabled_case,
)


class TestThinkingDisabledAcrossRoster:
    @pytest.mark.parametrize("model", roster_models()[::2])
    def test_roster_model_constructs_with_thinking_disabled(
        self, tmp_path: Path, model: str
    ) -> None:
        run_thinking_disabled_case(tmp_path, model)

    def test_unregistered_gemini_thinking_disabled_is_noop(self, tmp_path: Path) -> None:
        run_case(
            tmp_path,
            "thinking_unregistered_gemini_thinking_disabled_is_noop",
            {"GEMINI_API_KEY": "sk-test"},
        )

    def test_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level(
        self, tmp_path: Path
    ) -> None:
        run_case(
            tmp_path,
            "thinking_gemini_3_1_thinking_disabled_maps_to_lowest_declared_level",
            {"GEMINI_API_KEY": "sk-test"},
        )

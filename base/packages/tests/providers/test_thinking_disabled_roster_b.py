from __future__ import annotations

from pathlib import Path

import pytest

from base.packages.tests.providers.model_config_process import (
    roster_models,
    run_thinking_disabled_case,
)


class TestThinkingDisabledAcrossRoster:
    @pytest.mark.parametrize("model", roster_models()[1::2])
    def test_roster_model_constructs_with_thinking_disabled(
        self, tmp_path: Path, model: str
    ) -> None:
        run_thinking_disabled_case(tmp_path, model)

from __future__ import annotations

import pytest
from pydantic import ValidationError

from base.config.domains.daemon.settings import DaemonSettings


def test_personal_dream_defaults_match_design() -> None:
    fields = DaemonSettings.model_fields

    assert fields["personal_dream_enabled"].default is True
    assert fields["personal_dream_cooldown_seconds"].default == 604800
    assert fields["personal_dream_index_max_lines"].default == 150
    assert fields["personal_dream_max_files"].default == 40


def test_personal_dream_env_aliases_parse() -> None:
    config = DaemonSettings.model_validate(
        {
            "MEIVA_PERSONAL_DREAM_ENABLED": False,
            "MEIVA_PERSONAL_DREAM_COOLDOWN_SECONDS": 86400,
            "MEIVA_PERSONAL_DREAM_INDEX_MAX_LINES": 120,
            "MEIVA_PERSONAL_DREAM_MAX_FILES": 25,
        }
    )

    assert config.personal_dream_enabled is False
    assert config.personal_dream_cooldown_seconds == 86400
    assert config.personal_dream_index_max_lines == 120
    assert config.personal_dream_max_files == 25


@pytest.mark.parametrize(
    "alias",
    [
        "MEIVA_PERSONAL_DREAM_COOLDOWN_SECONDS",
        "MEIVA_PERSONAL_DREAM_INDEX_MAX_LINES",
        "MEIVA_PERSONAL_DREAM_MAX_FILES",
    ],
)
@pytest.mark.parametrize("bad_value", [0, -1])
def test_personal_dream_non_positive_limits_are_rejected(alias: str, bad_value: int) -> None:
    with pytest.raises(ValidationError):
        DaemonSettings.model_validate({alias: bad_value})


def test_personal_dream_fields_declare_agent_runner_cluster_pinned_metadata() -> None:
    for name in (
        "personal_dream_enabled",
        "personal_dream_cooldown_seconds",
        "personal_dream_index_max_lines",
        "personal_dream_max_files",
    ):
        extra = DaemonSettings.model_fields[name].json_schema_extra
        assert isinstance(extra, dict)
        assert extra["capability"] == "agent-runner"
        assert extra["scope"] == "cluster-pinned"
        assert extra["restart_required"] == "all"

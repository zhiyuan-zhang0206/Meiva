from __future__ import annotations

import pytest
from pydantic import ValidationError

from base.config.domains.daemon.settings import DaemonSettings


def test_workspace_hygiene_defaults_match_design() -> None:
    fields = DaemonSettings.model_fields

    assert fields["workspace_hygiene_enabled"].default is True
    assert fields["workspace_hygiene_max_items_per_dir"].default == 20
    assert fields["workspace_hygiene_remind_cooldown_seconds"].default == 86400


def test_workspace_hygiene_env_aliases_parse() -> None:
    config = DaemonSettings.model_validate(
        {
            "MEIVA_WORKSPACE_HYGIENE_ENABLED": False,
            "MEIVA_WORKSPACE_HYGIENE_MAX_ITEMS_PER_DIR": 15,
            "MEIVA_WORKSPACE_HYGIENE_REMIND_COOLDOWN_SECONDS": 3600,
        }
    )

    assert config.workspace_hygiene_enabled is False
    assert config.workspace_hygiene_max_items_per_dir == 15
    assert config.workspace_hygiene_remind_cooldown_seconds == 3600


@pytest.mark.parametrize(
    "alias",
    [
        "MEIVA_WORKSPACE_HYGIENE_MAX_ITEMS_PER_DIR",
        "MEIVA_WORKSPACE_HYGIENE_REMIND_COOLDOWN_SECONDS",
    ],
)
@pytest.mark.parametrize("bad_value", [0, -1])
def test_workspace_hygiene_non_positive_limits_are_rejected(alias: str, bad_value: int) -> None:
    with pytest.raises(ValidationError):
        DaemonSettings.model_validate({alias: bad_value})


def test_workspace_hygiene_fields_declare_agent_runner_cluster_pinned_metadata() -> None:
    for name in (
        "workspace_hygiene_enabled",
        "workspace_hygiene_max_items_per_dir",
        "workspace_hygiene_remind_cooldown_seconds",
    ):
        extra = DaemonSettings.model_fields[name].json_schema_extra
        assert isinstance(extra, dict)
        assert extra["capability"] == "agent-runner"
        assert extra["scope"] == "cluster-pinned"
        assert extra["restart_required"] == "all"

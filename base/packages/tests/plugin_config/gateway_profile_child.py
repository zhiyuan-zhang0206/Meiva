# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
from __future__ import annotations

import sys

import pytest

from base.paths import meiva_home


def gateway_overlay() -> None:
    from base.config import settings
    from base.packages.plugin_providers import build_model_catalog
    from base.packages.plugins.config_registration import validate_config_overlay
    from base.packages.plugins.plugin_errors import InvalidConfigOverlay

    assert not settings.has_domain("agent")
    models = build_model_catalog(home=meiva_home()).models
    if sys.argv[1] == "declaring-model":
        validate_config_overlay(
            {"completion_notice_policy": "hourly"}, configs={}, models=models, home=meiva_home()
        )
        with pytest.raises(InvalidConfigOverlay, match="completion_notice_policy"):
            validate_config_overlay(
                {"completion_notice_policy": "bogus"}, configs={}, models=models, home=meiva_home()
            )
    elif sys.argv[1] == "validators":
        validate_config_overlay(
            {"skills_to_expand_at_start": "a,b"}, configs={}, models=models, home=meiva_home()
        )
        validate_config_overlay(
            {"exec_timeout_seconds": 25.0}, configs={}, models=models, home=meiva_home()
        )
        with pytest.raises(InvalidConfigOverlay, match="only accepts"):
            validate_config_overlay(
                {"eval_network_allowlist": ["web", "shell"]},
                configs={},
                models=models,
                home=meiva_home(),
            )
        with pytest.raises(InvalidConfigOverlay, match="exec_node_timeout_seconds"):
            validate_config_overlay(
                {"exec_timeout_seconds": 1200.0},
                configs={},
                models=models,
                home=meiva_home(),
            )
    else:
        raise ValueError("unknown gateway overlay case")


gateway_overlay()

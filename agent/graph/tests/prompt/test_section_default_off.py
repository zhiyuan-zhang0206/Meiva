import os
from typing import Any

from agent.graph.prompt.system_prompt import build_system_prompt
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY
from base.paths import meiva_home

ENVIRON = dict(os.environ)


def _slices(**changes: Any) -> AgentSlices:
    return AgentSlices.resolve(default_reader=defaults(**changes))


def _full_prompt(**changes: Any) -> str:
    return build_system_prompt(
        EMPTY,
        _slices(**changes),
        agent_id=1,
        catalog=build_model_catalog(home=meiva_home()),
        home=meiva_home(),
        environ=ENVIRON,
    )


def test_output_shape_and_ui_delivery_default_off_and_explicit_on_injects():
    default = _full_prompt()
    assert "# Output shape" not in default
    assert "# Deliver through the UI" not in default
    enabled = _full_prompt(
        agent__prompt_output_conciseness_enabled=True, agent__prompt_ui_delivery_enabled=True
    )
    assert "# Output shape" in enabled
    assert "# Deliver through the UI" in enabled

# meiva:note
# A real plugin directory for the attachment tests. `write_sample_plugin` writes the manifest, the
# surface module and an `agent_runtime` face that declares the two state classes below into the
# test home's plugin directory, enables it and loads the face with the framework's own by-path
# loader, so the attachment's checkpoint read builds its agent state from the same discovery a
# production process uses. The caller removes the modules it loaded when the test ends.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from base.packages.plugins.enable_config import write_local
from base.packages.plugins.loading.plugin_loader import load_plugin_module
from base.paths import meiva_home

PLUGIN = "sample"
FACE_PREFIX = f"plugins.{PLUGIN}"


def _union(left: set[str], right: set[str]) -> set[str]:
    return left | right


class ExamplePlugin(BaseModel):
    seen: Annotated[set[str], _union] = Field(default_factory=set)


class ExampleMessagesPlugin(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list[AnyMessage])


_FACE = """\
from agent.tests.impersonation.external.sample_plugin import ExampleMessagesPlugin, ExamplePlugin
from base.packages.plugins.extensions import PluginContributions


def contribute() -> PluginContributions:
    return PluginContributions(state=(ExamplePlugin, ExampleMessagesPlugin))
"""


def write_sample_plugin(plugins_dir: Path) -> None:
    directory = plugins_dir / PLUGIN
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text("__description__ = 'attachment test plugin'\n")
    (directory / "agent_runtime.py").write_text(_FACE)
    (directory / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": PLUGIN,
                "version": "1.0.0",
                "engines": {"meiva": ">=0.1.0"},
                "contributions": {},
            }
        )
    )
    write_local({"plugins": {PLUGIN: {"enabled": True}}}, home=meiva_home())
    load_plugin_module(directory / "plugin.py", name=PLUGIN, pkg="plugins")
    load_plugin_module(
        directory / "agent_runtime.py", name=PLUGIN, pkg="plugins", module="agent_runtime"
    )


def forget_sample_plugin() -> None:
    for key in [name for name in sys.modules if name == "plugins" or name.startswith("plugins.")]:
        del sys.modules[key]

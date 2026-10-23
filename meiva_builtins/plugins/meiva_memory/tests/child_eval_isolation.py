"""Child process for the eval-isolation boot tests: install the memory plugin, then apply boot.

Usage: `child_eval_isolation.py <isolated|network_allowlist|off>`; prints `ok` on success.
"""

import os
import sys
from types import SimpleNamespace

import meiva
from agent.process_boot import apply_per_agent_eval_isolation
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.config.tests.configuration import defaults
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkNamespace
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install, process_context
from meiva_builtins.plugins.meiva_memory import plugin


def _install() -> None:
    install.install(
        ExtensionRegistry(
            (
                (
                    "tasks_namespace",
                    PluginContributions(
                        sdk_namespaces=(SdkNamespace("tasks", SimpleNamespace(list=list)),)
                    ),
                ),
                ("meiva_memory", plugin.contribute()),
            )
        ),
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
    )


def isolated() -> None:
    _install()
    assert hasattr(meiva, "tasks")

    original_path_doc = meiva.memory.PATH.__doc__
    os.environ["MEIVA_AGENT_ID"] = "417"
    process_context.bind_context(MeivaContext(identity=AgentIdentity(417, True)))
    apply_per_agent_eval_isolation(
        default_reader=defaults(agent__eval_isolation=True, agent__eval_network_allowlist=[])
    )

    assert not hasattr(meiva, "web")
    assert not hasattr(meiva, "understand")
    assert not hasattr(meiva, "mcps")
    assert not hasattr(meiva, "ui")
    assert not hasattr(meiva.agents, "get_last_message")
    assert not hasattr(meiva, "tasks")
    assert meiva.memory.PATH.name == "memory-pool"
    assert meiva.memory.PATH.parent.name == "417"
    assert meiva.memory.PATH.is_dir()
    assert meiva.memory.PATH.__doc__ == original_path_doc
    assert meiva.memory.search("shared result") == []
    assert not hasattr(meiva.memory, "search_detailed")
    print("ok")


def network_allowlist() -> None:
    _install()
    os.environ["MEIVA_AGENT_ID"] = "418"
    process_context.bind_context(MeivaContext(identity=AgentIdentity(418, True)))
    apply_per_agent_eval_isolation(
        default_reader=defaults(agent__eval_isolation=True, agent__eval_network_allowlist=["web"])
    )

    assert hasattr(meiva, "web")
    assert not hasattr(meiva, "understand")
    print("ok")


def off() -> None:
    _install()
    original_path = meiva.memory.PATH
    apply_per_agent_eval_isolation(default_reader=defaults(agent__eval_isolation=False))

    assert hasattr(meiva, "web")
    assert hasattr(meiva, "understand")
    assert hasattr(meiva, "mcps")
    assert hasattr(meiva, "ui")
    assert hasattr(meiva.agents, "get_last_message")
    assert original_path == meiva.memory.PATH
    print("ok")


{"isolated": isolated, "network_allowlist": network_allowlist, "off": off}[sys.argv[1]]()

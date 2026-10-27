# meiva:note
# Child process for the eval-isolation boot tests: install the memory plugin, then apply boot.
#
# Usage: `child_eval_isolation.py <isolated|isolated_in_other_home|network_allowlist|missing_agent|off> [home]`;
# prints `ok` on success. `isolated_in_other_home` takes the home to hand to the boot, which is not `MEIVA_HOME`.

import sys
from pathlib import Path
from types import SimpleNamespace

import meiva
from agent.process_boot import apply_per_agent_eval_isolation
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
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
        sampling=fixed_sampling_owner(),
    )


def isolated() -> None:
    _install()
    assert hasattr(meiva, "tasks")

    process_context.bind_context(
        MeivaContext(identity=AgentIdentity(417, True), meiva_home=resolve_meiva_home())
    )
    original_path_doc = meiva.memory.PATH.__doc__
    apply_per_agent_eval_isolation(
        agent_id=417,
        default_reader=defaults(agent__eval_isolation=True, agent__eval_network_allowlist=[]),
        home=resolve_meiva_home(),
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


def isolated_in_other_home() -> None:
    _install()
    other_home = Path(sys.argv[2])
    process_context.bind_context(
        MeivaContext(identity=AgentIdentity(417, True), meiva_home=resolve_meiva_home())
    )
    apply_per_agent_eval_isolation(
        agent_id=417,
        default_reader=defaults(agent__eval_isolation=True, agent__eval_network_allowlist=[]),
        home=other_home,
    )

    assert other_home / "workspaces" / "417" / "memory-pool" == meiva.memory.PATH
    assert not (resolve_meiva_home() / "workspaces" / "417").exists()
    print("ok")


def network_allowlist() -> None:
    _install()
    process_context.bind_context(
        MeivaContext(identity=AgentIdentity(418, True), meiva_home=resolve_meiva_home())
    )
    apply_per_agent_eval_isolation(
        agent_id=418,
        default_reader=defaults(agent__eval_isolation=True, agent__eval_network_allowlist=["web"]),
        home=resolve_meiva_home(),
    )

    assert hasattr(meiva, "web")
    assert not hasattr(meiva, "understand")
    print("ok")


# meiva:note
# The scenario `missing_agent`: isolation is on but the exec carries no agent id, which is refused.
def missing_agent() -> None:
    _install()
    try:
        apply_per_agent_eval_isolation(
            agent_id=None,
            default_reader=defaults(agent__eval_isolation=True, agent__eval_network_allowlist=[]),
            home=resolve_meiva_home(),
        )
    except RuntimeError as error:
        assert "agent identity" in str(error)
        print("ok")
        return
    raise AssertionError("eval isolation without an agent id was accepted")


def off() -> None:
    _install()
    process_context.bind_context(MeivaContext(meiva_home=resolve_meiva_home()))
    original_path = meiva.memory.PATH
    apply_per_agent_eval_isolation(
        agent_id=None,
        default_reader=defaults(agent__eval_isolation=False),
        home=resolve_meiva_home(),
    )

    assert hasattr(meiva, "web")
    assert hasattr(meiva, "understand")
    assert hasattr(meiva, "mcps")
    assert hasattr(meiva, "ui")
    assert hasattr(meiva.agents, "get_last_message")
    assert original_path == meiva.memory.PATH
    print("ok")


{
    "isolated": isolated,
    "isolated_in_other_home": isolated_in_other_home,
    "network_allowlist": network_allowlist,
    "missing_agent": missing_agent,
    "off": off,
}[sys.argv[1]]()

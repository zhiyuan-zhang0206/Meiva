# meiva:note
# Child process for the SDK-disable install test: a namespace the disable machinery stubbed out
# is framework-owned, so the plugin that declares it is refused and the sentinel stays.
#
# Prints one JSON line: the refused plugin, the failure class, the message fragment and whether
# the sentinel survived.


import json
import sys
from types import SimpleNamespace

import meiva
from base.agents.sdk import call_policy
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkNamespace
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install, sdk_disable
from meiva.sdk_surface.sdk_disable import DisabledSDKModule

failures: list[tuple[str, BaseException]] = []


def collect(name: str, exc: BaseException) -> None:
    failures.append((name, exc))


def ping() -> str:
    return "pong"


sdk_disable.apply_entries(["disabled_ns"])
sentinel = sys.modules["meiva.disabled_ns"]
assert isinstance(sentinel, DisabledSDKModule)
sampling = call_policy.SamplingPolicyOwner(reader=call_policy.SamplingPolicy)
registry = ExtensionRegistry(
    (
        (
            "wants_disabled",
            PluginContributions(
                sdk_namespaces=(
                    SdkNamespace(
                        "disabled_ns",
                        SimpleNamespace(__doc__="An installed test namespace.", ping=ping),
                    ),
                )
            ),
        ),
    )
)
admitted = install.install(
    registry,
    collect,
    config_images=DiskPluginConfigImages(resolve_meiva_home()),
    sampling=sampling,
)
[(name, exc)] = failures
install.uninstall()
sampling.close()
print(
    json.dumps(
        {
            "plugin": name,
            "failure": type(exc).__name__,
            "mentions_disable": "MEIVA_SDK_DISABLE" in str(exc),
            "admitted": [plugin for plugin, _contributions in admitted.plugins],
            "sentinel_kept": sys.modules["meiva.disabled_ns"] is sentinel,
            "namespace_absent": not hasattr(meiva, "disabled_ns"),
        }
    )
)

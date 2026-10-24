# meiva:note
# Imports the exec subprocess's face of the plugin in a clean interpreter and prints which modules
# that import loaded: the decision-call stack, services, the agent runtime and langchain/langgraph
# (the absolute prefixes) must stay out, and the plugin may add no httpx on top of what
# `import meiva` already loads, since `import meiva` itself loads httpx for the gateway client's
# transport.

import json
import sys

import meiva

loaded_with_meiva = set(sys.modules)

ABSOLUTE = (
    "base.lm.decision.call",
    "base.lm.catalog.decide.http",
    "services.",
    "agent.",
    "langchain",
    "langgraph",
)


def import_faces() -> list[str]:
    from meiva_builtins.plugins.meiva_memory import plugin, written_notes

    return [plugin.__name__, written_notes.__name__]


faces = import_faces()
added = set(sys.modules) - loaded_with_meiva
print(
    json.dumps(
        {
            "imported": [meiva.__name__, *faces],
            "absolute": sorted(n for n in sys.modules if n.startswith(ABSOLUTE)),
            "added_httpx": sorted(n for n in added if n.startswith("httpx")),
        }
    )
)

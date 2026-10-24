# meiva:note
# Child process for the metering identity test: an unreadable caller profile rejects admission.
#
# Usage: `child_metering_identity.py <sync|async|mcp>`; the parent sets `MEIVA_CALLER_IDENTITY`.
# Prints one JSON line: whether the call was rejected and which bodies ran.


import asyncio
import json
import sys

from base.agents.sdk import call_policy
from meiva.sdk_surface import metering

owner = call_policy.SamplingPolicyOwner(reader=call_policy.SamplingPolicy)
executed: list[str] = []


async def async_body() -> None:
    executed.append("async")


def mcp_body(*_args: object) -> None:
    executed.append("mcp")


def run(kind: str) -> None:
    if kind == "async":
        asyncio.run(metering.make_recorder(async_body, "probe.async", owner, None)())
    elif kind == "mcp":
        metering.make_mcp_recorder(mcp_body, owner, None)(None, "probe", "tool")
    else:
        metering.make_recorder(lambda: executed.append("sync"), "probe.sync", owner, None)()


rejected = False
try:
    run(sys.argv[1])
except ValueError:
    rejected = True
finally:
    owner.close()
print(json.dumps({"rejected": rejected, "executed": executed}))

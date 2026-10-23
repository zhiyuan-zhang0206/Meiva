import asyncio
import contextlib
import io
import sys

from agent.graph.node_log._plan import StallConfig
from agent.graph.node_log._stall import stall_guard

_ARMED = StallConfig(threshold_s=0.05, thread_dump_stagger_s=0.1)
_BODY_S = 0.6


async def main(mode: str) -> None:
    if mode == "stalled":
        with stall_guard("llm", _ARMED):
            await asyncio.sleep(_BODY_S)
    elif mode == "leaked-redirect":
        with contextlib.redirect_stderr(io.StringIO()), stall_guard("llm", _ARMED):
            await asyncio.sleep(_BODY_S)
    elif mode == "exempt-node":
        with stall_guard("claim", _ARMED):
            await asyncio.sleep(_BODY_S)
    elif mode == "disabled":
        with stall_guard("llm", StallConfig(threshold_s=0.0, thread_dump_stagger_s=0.1)):
            await asyncio.sleep(_BODY_S)
    elif mode == "fast-body-then-quiet":
        with stall_guard("llm", _ARMED):
            pass
        await asyncio.sleep(_BODY_S)
    elif mode == "failed-body-then-quiet":
        with contextlib.suppress(RuntimeError), stall_guard("llm", _ARMED):
            raise RuntimeError("body failed")
        await asyncio.sleep(_BODY_S)
    else:
        raise SystemExit(f"unknown mode {mode!r}")


asyncio.run(main(sys.argv[1]))

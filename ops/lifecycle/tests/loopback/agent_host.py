# meiva:note
# The local agent host as the lifecycle ops see it: a loopback server answering `POST /cancel-turn`.
#
# `serve_agent_host` stands up the host's health port on port 0 and hands the lifecycle op the
# `LocalAgentHost` that names it, so the op dials it through its real transport. `cancelled` is
# the answer ("a turn was running"), and `on_cancel` runs what the host would do while it cancels
# (the agent allocating one more shell session, for instance) before the answer goes out.

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ops.lifecycle.hosted_turn import LocalAgentHost
from ops.lifecycle.tests.loopback.scripted_post import Reply, ServedPost, serve_post

__all__ = ["ServedAgentHost", "serve_agent_host"]


# meiva:note
# The running host: the value that reaches it, and the cancel requests it has been sent.
@dataclass(frozen=True)
class ServedAgentHost:
    host: LocalAgentHost
    served: ServedPost

    @property
    def cancel_requests(self) -> list[int]:
        return [int(str(posted.body["agent_id"])) for posted in self.served.posted]


# meiva:note
# Serve the cancel route under `home` until the block ends.
@contextmanager
def serve_agent_host(
    home: Path, *, cancelled: bool = True, on_cancel: Callable[[int], None] | None = None
) -> Generator[ServedAgentHost]:
    def cancel(body: dict[str, object]) -> Reply:
        if on_cancel is not None:
            on_cancel(int(str(body["agent_id"])))
        return Reply({"cancelled": cancelled})

    with serve_post({"/cancel-turn": cancel}) as served:
        yield ServedAgentHost(LocalAgentHost(home=home, health_port=served.port), served)

# meiva:note
# The request deadline, not the backend's own timeout, names a timed-out search.
#
# On 2026-11-06 CI, with the search deadline and the vector backend's HTTP client timeout both
# 0.3s, a loaded shard answered `memory search backend failed: ` — the backend client's timeout,
# which carries no message — where an idle machine answered `exceeded its 0.3s deadline` for the
# same run. Both timers came due in one scheduling turn; the backend's timeout is the inner
# cancel scope, so it absorbs the request's cancellation and reports its own. The backend is the
# backstop of the request deadline, not a peer of it: its timeout is the deadline plus the
# configured grace, and the message that names the request wins.
#
# `_mirror_load` is the injection that makes this deterministic instead of load-dependent: the
# test blocks the event loop the way a busy shard does, which is what brings the two timers due
# together. Run the same test with `MEIVA_MEMORY_SEARCH_BACKEND_DEADLINE_GRACE_SECONDS=0` (the
# tied case) and it answers `memory search backend failed: ` instead — that is the reverse
# control for this fix.
# meiva:link gateway/routers/docs/memory.meiva.okf.md

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from base.config import settings
from gateway.tests.routers.memory_search.search_support import (
    SearchRig,
    asgi_client,
    memory_config,
    post_memory_search,
    stalled_search,
)
from gateway.tests.routers.memory_search.search_support import rig as rig

# meiva:note
# The search deadline of a search whose backend never answers, and the loop stall that puts the
# request deadline and the backend timeout in one scheduling turn.
_DEADLINE_S = 0.3
_LOOP_STALL_S = 0.4


# meiva:note
# Hold the event loop the way a loaded CI shard holds it. This is the injection, not a sleep to
# let something settle: the request deadline is due when the loop resumes, and so is the backend
# timeout unless the grace keeps it clear of this window.
def _mirror_load(seconds: float) -> None:
    time.sleep(seconds)


# meiva:note
# Wait until the vector service has the pending search, so the stall starts after the backend's
# own timeout is armed.
async def _wait_until_the_backend_is_asked(rig: SearchRig, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not rig.service.asked and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert rig.service.asked, "the search never reached the vector service"


async def test_a_stalled_backend_is_reported_as_the_request_deadline(
    tmp_path: Path, rig: SearchRig
) -> None:
    app = stalled_search(rig, tmp_path, memory_search_deadline_seconds=_DEADLINE_S)

    async with asgi_client(app) as client:
        request = asyncio.create_task(post_memory_search(client))
        await _wait_until_the_backend_is_asked(rig)
        _mirror_load(_LOOP_STALL_S)
        response = await asyncio.wait_for(request, timeout=10)

    assert response.status_code == 503
    assert response.json()["reason"] == "indexer_unavailable"
    assert response.json()["detail"] == f"memory search exceeded its {_DEADLINE_S:g}s deadline"


# meiva:note
# The default is the reasoned value, and zero — the tied case — is the one this setting exists
# to remove.
def test_the_shipped_grace_keeps_the_backend_timeout_strictly_later() -> None:
    field = type(settings.services).model_fields["memory_search_backend_deadline_grace_seconds"]
    assert field.default == 0.5


def test_the_configured_grace_reaches_the_search_settings(tmp_path: Path) -> None:
    config = memory_config(tmp_path, memory_search_backend_deadline_grace_seconds=1.5)

    assert config.memory_search.backend_deadline_grace == 1.5

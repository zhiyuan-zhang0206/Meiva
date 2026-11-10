from __future__ import annotations

from base.lm.harness.codex.frames import JsonObject


# meiva:note
# The request that clones a thread for a fork. `excludeTurns` keeps the response small: a long-lived
# thread's history would otherwise come back on one line (the same reason `thread/resume` replies
# reach tens of MiB), and the clone is resumed by the ordinary open request right after, which
# carries the execution policy. Only the source thread and the working directory are sent: the
# clone keeps the source's model and instructions, so its first request shares the static prefix
# the source warmed (the history itself is not cached on a first Codex turn).
# meiva:link base/lm/harness/docs/fork.md
def fork_thread_request(source_thread_id: str, *, cwd: str) -> tuple[str, JsonObject]:
    return ("thread/fork", {"threadId": source_thread_id, "cwd": cwd, "excludeTurns": True})

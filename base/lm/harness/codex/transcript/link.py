from __future__ import annotations

from collections.abc import Callable, Mapping

from base.lm.harness.codex.frames import History

# meiva:note
# The one thing a Codex session shares with the reader of its own transcript: the app-server the session already
# runs. The session attaches its connection when it opens its thread and detaches it when it closes or the server
# is gone, and gives every notification it receives to `on_notification`; the reader asks for the connection
# (`history`) at each read and for what the notifications said was active. Nothing else crosses: the reader never
# starts, closes or configures the app-server, and the session never reads the transcript.
#
# The connection is the session's own, so a reader's request is one more JSON-RPC call on it. The transport
# correlates replies by request id (the ids come from the one counter of the transport, so a reader's can never
# collide with a turn's), writes each frame under one lock, and answers a request when its reply arrives, whatever
# else is in flight; a reader's call therefore waits for its own reply and for nothing the running turn does. It
# is bounded by the reader's own timeout, and a page is bounded by `PAGE_ITEMS`, so a slow or large answer holds
# only the reader.
#
# Notifications carry no content for the reader. A whitelisted notification (an item or turn event, a token usage
# update) bumps the activity of the thread it names, which is how the reader learns a child thread exists and is
# working; the ones that report something finished (`item/completed`, `turn/completed`, `turn/started`,
# `thread/tokenUsage/updated`) also wake the writer. `rollout_hint` is the main thread's rollout path as the
# server reported it when the thread was opened.
# meiva:link base/lm/harness/codex/docs/codex.md

_ACTIVE_PREFIXES = ("item/", "turn/", "thread/tokenUsage/")
_WAKING = frozenset(
    {"item/completed", "turn/completed", "turn/started", "thread/tokenUsage/updated"}
)


class TranscriptLink:
    def __init__(self, wake: Callable[[str | None], None]) -> None:
        self._wake = wake
        self._history: History | None = None
        self._rollout_hint: str | None = None
        self._cli_version: str | None = None
        self._activity: dict[str, int] = {}

    def attach(
        self, history: History, *, rollout_path: str | None, cli_version: str | None
    ) -> None:
        self._history = history
        self._rollout_hint = rollout_path
        self._cli_version = cli_version

    def detach(self) -> None:
        self._history = None

    def history(self) -> History | None:
        return self._history

    @property
    def rollout_hint(self) -> str | None:
        return self._rollout_hint

    @property
    def cli_version(self) -> str | None:
        return self._cli_version

    def activity(self) -> Mapping[str, int]:
        return self._activity

    def on_notification(self, method: str, params: Mapping[str, object]) -> None:
        if not method.startswith(_ACTIVE_PREFIXES):
            return
        thread = params.get("threadId")
        if not isinstance(thread, str) or not thread:
            return
        self._activity[thread] = self._activity.get(thread, 0) + 1
        if method in _WAKING:
            self._wake(thread)

# meiva:note
# When a streamed answer is finished. Completion is judged on text stability, not on a per-site spinner: every site marks "still generating" with its own stop-button selector and those drift, so an answer is done once its text stops changing for a few seconds AND no known stop button is visible. If the stop selector has drifted (it matches nothing) text stability alone still converges. The driver observes the page and calls `fold_poll`; this module holds the verdict, as a value over the `now` the driver reads once per look.
from __future__ import annotations

from dataclasses import dataclass, replace


# meiva:note
# One owned tab to poll together with others: the site it chats with and its page id.
@dataclass(frozen=True)
class Tab:
    site: str
    page_id: int


# meiva:note
# The answer a tab has produced so far: the newest text seen (the best partial on a timeout) and whether it settled.
@dataclass(frozen=True)
class Answer:
    text: str
    complete: bool


# meiva:note
# The idle detector of one streamed answer. `last` is the newest text seen, `stable_since` the time the text last went stable with no stream control visible (None while it changes or streams), `saw_stream` whether a stop control was ever visible, `growth_steps` how often the text grew (two or more is a real stream even when the stop selector drifted), and `settled` whether the answer is finished.
@dataclass(frozen=True)
class IdleState:
    last: str = ""
    stable_since: float | None = None
    saw_stream: bool = False
    growth_steps: int = 0
    settled: bool = False

    @property
    def answer(self) -> Answer:
        return Answer(text=self.last, complete=self.settled)


# meiva:note
# Fold one look at a tab (its last answer text and whether a stop control is visible) into the detector. The answer settles once the text is non-empty, unchanged for `stable_secs`, with no stop control visible during that whole window, AND a real answer actually streamed in (the stop control was seen, or the text grew more than once): that last guard stops a static pre-first-token placeholder from being read as a finished answer. A look that shows the stop control restarts the stability window, so a control that flickers off for one look between two phases of an answer cannot complete it early.
def fold_poll(
    state: IdleState, *, text: str, streaming: bool, now: float, stable_secs: float
) -> IdleState:
    if state.settled:
        return state
    seen = replace(state, saw_stream=state.saw_stream or streaming)
    if text != state.last:
        grew = 1 if len(text) > len(state.last) else 0
        return replace(seen, last=text, stable_since=None, growth_steps=state.growth_steps + grew)
    if streaming:
        return replace(seen, stable_since=None)
    if not text or not (seen.saw_stream or seen.growth_steps >= 2):
        return seen
    if seen.stable_since is None:
        return replace(seen, stable_since=now)
    return replace(seen, settled=now - seen.stable_since >= stable_secs)

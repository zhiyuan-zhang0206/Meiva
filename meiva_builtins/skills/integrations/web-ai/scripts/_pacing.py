# meiva:note
# How long the driver waits, and when a wait is over. Every pause and deadline of the shell is a length in seconds in `Pacing`, whose defaults are the values in use; the shell sleeps, a test passes small values.
from __future__ import annotations

from dataclasses import dataclass


# meiva:note
# Every wait and settle of the driver, in seconds; the defaults are the values in use. `page_settle` is the pause after a fresh chat page opens, `composer_wait` how long the composer may take to render, `editor_settle` the beat before typing (the editor's handlers attach a beat after the node renders), `login_click_gap` and `login_wait` the sign-in walk, `login_probe_wait` how long a login probe waits for the composer, `ready_poll` the look interval of the composer and attach probes, `answer_stable` how long the answer text must stay unchanged, `answer_poll` the look interval while an answer streams, and the `attach_*` and `upload_*` fields the file attach. `download_poll` and `download_recheck` pace a browser download collected from the Downloads folder.
@dataclass(frozen=True)
class Pacing:
    page_settle: float = 2.5
    composer_wait: float = 30.0
    editor_settle: float = 0.4
    login_click_gap: float = 1.5
    login_wait: float = 15.0
    login_probe_wait: float = 8.0
    ready_poll: float = 0.5
    answer_stable: float = 3.5
    answer_poll: float = 0.7
    attach_click_gap: float = 0.8
    upload_wait: float = 90.0
    upload_poll: float = 1.0
    download_poll: float = 1.0
    download_recheck: float = 1.0


STANDARD_PACE = Pacing()


# meiva:note
# How long to sleep before the next look, or None once `now` has reached `deadline` and the wait is over. The delay is always the full `poll`: a deadline that falls inside it is noticed at the look after it.
def next_poll_delay(*, now: float, deadline: float, poll: float) -> float | None:
    if now >= deadline:
        return None
    return poll

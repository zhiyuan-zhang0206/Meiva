# meiva:note
# The values the driver's ask entry points exchange with their callers, and the decisions that shape them: which message to send where, what a finished or failed ask reports, and which owned tabs a panel still has to close. The driver opens, polls and closes tabs; this module only says what to report and which ids are left over.
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from _idle import Answer
from _sites import chat_id_from_url, site

UNIDENTIFIED_TAB_ERROR = "RuntimeError: could not identify the opened tab (page-list format drift)"


# meiva:note
# One message to send: the site to ask, the prompt, an optional local file to attach, and an optional conversation URL to continue instead of opening a fresh chat.
@dataclass(frozen=True)
class AskSpec:
    name: str
    prompt: str
    file: str | Path | None = None
    conversation_url: str | None = None


# meiva:note
# The row of an ask that reached its answer step. `answer` is the best text captured and `complete` says whether it settled in time (always False when the ask did not wait). `url` is the durable handle to resume the conversation, `chat_id` its stable identifier, and `tab_kept` whether the owned tab was left open.
class AskRow(TypedDict):
    site: str
    label: str
    submitted_via: str
    answer: str
    complete: bool
    url: str
    chat_id: str | None
    tab_kept: bool


# meiva:note
# The row of a panel member whose setup failed. It is a row instead of a raise so one dead model never sinks the rest of the panel.
class FailedRow(TypedDict):
    site: str
    label: str
    error: str


# meiva:note
# The row of an ask for site `name`: how it was submitted, the answer so far, the conversation URL read before any tab was closed (closing moves the selection off the tab) and whether the tab stays open.
def answer_row(*, name: str, via: str, answer: Answer, url: str, tab_kept: bool) -> AskRow:
    return {
        "site": name,
        "label": site(name).label,
        "submitted_via": via,
        "answer": answer.text,
        "complete": answer.complete,
        "url": url,
        "chat_id": chat_id_from_url(name, url),
        "tab_kept": tab_kept,
    }


# meiva:note
# The row of a panel member for site `name` whose setup raised or whose tab could not be identified.
def failed_row(name: str, error: str) -> FailedRow:
    return {"site": name, "label": site(name).label, "error": error}


# meiva:note
# Whether an ask leaves its tab open. A tab is kept when the caller asked for it, and always when the ask did not wait: the answer is still streaming and gets collected later through the conversation URL.
def keeps_tab(*, wait: bool, keep_tab: bool) -> bool:
    return keep_tab or not wait


# meiva:note
# The owned tabs a panel still has to close once it stops, in setup order: every live tab that was not already closed, none at all when the caller keeps the tabs. A tab closed in the collect loop is never closed twice.
def tabs_left_open(
    live_page_ids: Collection[int], closed: Collection[int], *, keep_tab: bool
) -> list[int]:
    if keep_tab:
        return []
    return [page_id for page_id in live_page_ids if page_id not in closed]

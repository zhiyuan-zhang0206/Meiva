# meiva:note
# The decisions of the model-based injection check, over values: whether a fetched page is
# judged at all, how its text is cut into model-sized pieces, and what finding a verdict
# yields. The checker (`injection_check.py`) reads the process and calls the decision model;
# these functions only choose, so the order of the gates and the cut points are tested
# directly.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from collections.abc import Collection
from enum import Enum, auto

_MAX_URL_CHARS = 200


class CheckStep(Enum):
    SKIP = auto()
    UNKNOWN_MODEL = auto()
    RUN = auto()


def check_text(title: str, content: str) -> str:
    return f"{title}\n\n{content}" if title else content


def plan_check(
    *,
    enabled: bool,
    delivery_active: bool,
    text: str,
    model: str,
    decision_models: Collection[str],
) -> CheckStep:
    if not enabled or not delivery_active or not text.strip():
        return CheckStep.SKIP
    if model not in decision_models:
        return CheckStep.UNKNOWN_MODEL
    return CheckStep.RUN


def finding_source(url: str, probability: float | None, threshold: float) -> str | None:
    if probability is None or probability < threshold:
        return None
    return f"web.fetch ({' '.join(url.split())[:_MAX_URL_CHARS]})"


# meiva:note
# Split `text` into consecutive pieces of at most `max_tokens` by `estimate_tokens`. A cut falls
# on a line break when one exists in the last fifth of the piece, otherwise mid-line. The pieces
# joined give back `text`. A budget below one token is a caller bug and raises ValueError.
def split_for_model(text: str, max_tokens: int) -> list[str]:
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be at least 1, got {max_tokens}")
    # meiva:note
    # estimate_tokens = ceil(ascii / 4) + non-ascii, so in quarter-token units an ASCII
    # character costs 1 and any other character 4.
    limit = max_tokens * 4
    pieces: list[str] = []
    start = 0
    while start < len(text):
        used = 0
        end = start
        while end < len(text):
            cost = 1 if ord(text[end]) < 128 else 4
            if used + cost > limit:
                break
            used += cost
            end += 1
        if end < len(text):
            newline = text.rfind("\n", start + (end - start) * 4 // 5, end)
            if newline > start:
                end = newline + 1
        pieces.append(text[start:end])
        start = end
    return pieces

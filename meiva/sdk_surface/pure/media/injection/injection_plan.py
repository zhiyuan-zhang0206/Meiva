# meiva:note
# The decisions of the model-based injection check, over values: whether a fetched page is
# judged at all and what finding a verdict yields. The checker (`injection_check.py`) reads the
# process and calls the decision model; these functions only choose, so the order of the gates is
# tested directly. How the page is cut and how the answers fold lives in `injection_pieces.py`.
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


# meiva:note
# The refusal for a configured model that is not a decision model of the catalog, naming the
# models that are.
def unknown_model_message(model: str, decision_models: Collection[str]) -> str:
    return f"web_fetch_injection_model {model!r} is not a decision model; available: " + ", ".join(
        sorted(decision_models)
    )


def finding_source(url: str, probability: float | None, threshold: float) -> str | None:
    if probability is None or probability < threshold:
        return None
    return f"web.fetch ({' '.join(url.split())[:_MAX_URL_CHARS]})"

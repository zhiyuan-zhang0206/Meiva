# meiva:note
# The complete `google.genai` import graph, entered under this module's own import lock; the race
# and the rule are in `anthropic_sdk.py`. A thread entering this SDK beside another one raised
# `google.genai.errors._DeadlockError` instead of a partially initialized module, so it follows the
# same rule. `APIError` is re-exported because `google.genai` forwards it through `__getattr__`,
# which the type checker reads as `Any`; the submodule import here runs after the root import above
# it completed, so it cannot race.
# meiva:link base/lm/compat/docs/compat.meiva.okf.md

from google import genai
from google.genai.errors import APIError

__all__ = ["APIError", "genai"]

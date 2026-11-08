# meiva:note
# The complete `openai` import graph, entered under this module's own import lock; the race and the
# rule are in `anthropic_sdk.py`. `base/lm/errors.py` reads this SDK while other turns are failing,
# which is exactly the moment several threads reach it together.
# meiva:link base/lm/compat/docs/compat.meiva.okf.md

import openai

__all__ = ["openai"]

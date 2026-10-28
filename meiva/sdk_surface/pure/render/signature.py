# meiva:note
# How a callable's signature reads in the SDK stubs, decided over the callable the renderer found:
# quoted annotations lose their quotes, an enum member `<Mode.X: 1>` shows as `Mode.X` and
# `typing.` prefixes are dropped; `(...)` for anything that is not callable or has no signature to
# read. The help renderer (`help.py`) prints what this returns.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

import inspect
import re


def format_signature(target: object) -> str:
    if not callable(target):
        return "(...)"
    try:
        sig = str(inspect.signature(target))
    except (ValueError, TypeError):
        return "(...)"
    sig = re.sub(r"(: |-> )'([^']*)'(?=[,)= ]|$)", r"\1\2", sig)
    sig = re.sub(r"<(\w+(?:\.\w+)+): [^<>]*>", r"\1", sig)
    return sig.replace("typing.", "")

"""Forced-compaction request text, independent of standing SDK exposure."""

from inspect import cleandoc

from base.agents.to_user.models import ONGOING_FILENAME

ONGOING_COMPACT_REMINDER = (
    f"If you maintain `{ONGOING_FILENAME}`, remove completed items and verify that "
    "each wait under `## Waiting` still applies."
)


# meiva:note
# The SDK-owned contract (the docstring of `meiva.self.compact`) as a summary model that cannot
# call tools reads it. The caller hands in the docstring it read, so this module imports no SDK
# shell.
def compact_contract(doc: str | None) -> str:
    if doc is None:
        raise RuntimeError("compaction requires its SDK contract")
    return cleandoc(doc)

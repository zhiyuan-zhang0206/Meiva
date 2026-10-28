"""Forced-compaction request text, independent of standing SDK exposure."""

from inspect import cleandoc


# meiva:note
# The SDK-owned contract (the docstring of `meiva.self.compact`) as a summary model that cannot
# call tools reads it. The caller hands in the docstring it read, so this module imports no SDK
# shell.
def compact_contract(doc: str | None) -> str:
    if doc is None:
        raise RuntimeError("compaction requires its SDK contract")
    return cleandoc(doc)

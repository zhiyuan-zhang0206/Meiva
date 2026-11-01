# meiva:note
# The SDK reads the Meiva home from the context it is bound to. A context bound before a test's
# unit home was pointed at carries the home of its own time, so a test whose subject reads the SDK
# binds the context again over the unit home it writes to: what the test puts under its home is what
# the SDK finds. A directory's conftest imports `context_over_unit_home` for the tests that need it.

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from base.agents.context import MeivaContext
from meiva.sdk_surface import process_context

__all__ = ["context_over_unit_home"]


@pytest.fixture(autouse=True)
def context_over_unit_home(unit_home: Path) -> Iterator[Path]:
    previous = process_context.bound_context()
    context = MeivaContext() if previous is None else previous
    environ = context.environ if context.environ is not None else dict(os.environ)
    process_context.bind_context(replace(context, meiva_home=unit_home, environ=environ))
    try:
        yield unit_home
    finally:
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)

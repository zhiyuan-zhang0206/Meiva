# meiva:note
# Explicit executable identity for the single start lifecycle, and the site it acts on.
#
# A start runs the checkout that loaded this code, with the interpreter that loaded it.
# The identity is verified once, where the entry admits the start, and held as a value:
# it does not grant migration, writer closure, or any other operation right. `HomeSite`
# is the home and checkout `meiva init` and `meiva start` are handed by the entry.

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from base.host import runtime_interpreter
from base.host.runtime_interpreter import LoadedRuntimeIdentity


# meiva:note
# The home one `meiva init` or `meiva start` acts on, and the checkout whose CLI runs it.
#
# `cli.main` reads both once, after the checkout gate (`cli.preflight`) has accepted this
# checkout for this home; init and start take them as values and read neither the
# environment nor `__file__`.
@dataclass(frozen=True)
class HomeSite:
    home: Path
    checkout: Path


# meiva:note
# The checkout, working directory and interpreter one start executes, with the loaded
# identity verified for that checkout when the start was admitted.
#
# `loaded` is what the serving root must report once the start publishes serving
# (`start_serving.mark_serving`): a source change between admission and launch makes the
# two differ and the start fails closed. The class only holds the value; `admit` is the one
# production constructor, and a test builds an instance around the identity it observes.
@dataclass(frozen=True)
class StartRuntime:
    code_root: Path
    cwd: Path
    interpreter: Path
    loaded: LoadedRuntimeIdentity

    # meiva:note
    # Verify that `checkout` is the canonical checkout loaded into this process and capture
    # its identity. Raises `LoadedRuntimeError` (a `ValueError`) when it is not.
    @classmethod
    def admit(cls, checkout: Path) -> StartRuntime:
        loaded = runtime_interpreter.verify_loaded_source(checkout)
        return cls(checkout, checkout, Path(sys.executable).absolute(), loaded)

    def module_argv(self, module: str, *arguments: str) -> list[str]:
        return [str(self.interpreter), "-m", module, *arguments]

# meiva:note
# Explicit executable identity for the single start lifecycle, and the site it acts on.
#
# A start runs the checkout that loaded this code, with the interpreter that loaded it.
# The identity is verified where the entry admits the start and held as a value, and the
# source digest in it is read again when the start publishes serving: it does not grant
# migration, writer closure, or any other operation right. `HomeSite` is the home and checkout
# `meiva init` and `meiva start` are handed by the entry.

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
# `loaded` is the identity at admission. The serving root reports the identity of the source
# it was launched from, and a start's own converge can legitimately change that source (a file
# it writes in the checkout) before the root launches, so `mark_serving` is handed
# `identity_now()`: a source change after the root launched makes the two differ and the start
# fails closed. `admit` is the one production constructor, and a test builds an instance around
# a checkout of its own.
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
        loaded = runtime_interpreter.verify_loaded_source(
            checkout, loaded=runtime_interpreter.loaded_runtime()
        )
        return cls(checkout, checkout, Path(sys.executable).absolute(), loaded)

    # meiva:note
    # The loaded identity with the source digest of `code_root` read again now.
    #
    # Everything else in the identity (interpreter, prefix, working directory) is a property of
    # this process and cannot change under a start; only the source can.
    def identity_now(self) -> LoadedRuntimeIdentity:
        return self.loaded.model_copy(
            update={"source_digest": runtime_interpreter.source_digest(self.code_root)}
        )

    def module_argv(self, module: str, *arguments: str) -> list[str]:
        return [str(self.interpreter), "-m", module, *arguments]

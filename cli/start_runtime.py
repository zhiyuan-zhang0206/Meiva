# meiva:note
# Explicit executable identity for the single start lifecycle, and the site it acts on.
#
# A start runs the checkout that loaded this code, with the interpreter that loaded
# it. The identity is captured once and rechecked before each lifecycle phase; it
# does not grant migration, writer closure, or any other operation right. `HomeSite`
# is the home and checkout `meiva init` and `meiva start` are handed by the entry.

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from base.host import runtime_interpreter
from base.host.runtime_interpreter import LoadedRuntimeIdentity


class StartRuntimeChangedError(ValueError):
    """The captured start runtime no longer matches the executing checkout."""


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


@dataclass(frozen=True)
class StartRuntime:
    """The checkout, working directory and interpreter one start executes."""

    code_root: Path
    cwd: Path
    interpreter: Path

    @classmethod
    def development(cls, checkout: Path) -> StartRuntime:
        return StartRuntime(checkout, checkout, Path(sys.executable).absolute())

    def identity(self) -> LoadedRuntimeIdentity:
        return runtime_interpreter.verify_loaded_source(self.code_root)

    def module_argv(self, module: str, *arguments: str) -> list[str]:
        return [str(self.interpreter), "-m", module, *arguments]

    def validate(self) -> None:
        """Recheck the captured runtime before a lifecycle phase consumes its paths."""
        if self != self.development(self.code_root):
            raise StartRuntimeChangedError("development start runtime changed")

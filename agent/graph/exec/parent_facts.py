import os
import sys
from pathlib import Path

from base.deploy.release import editable_install

from ._child_boundary import ParentFacts


# meiva:note
# What this process looks like at the moment it spawns a child: the environment the child
# inherits, its working directory, the interpreter it re-executes, the checkout that
# interpreter belongs to and the one extra checkout its editable install may name. The
# composition root reads it once and hands it to `SubprocessExecProcess`.
def read_parent_facts() -> ParentFacts:
    return ParentFacts(
        environ=dict(os.environ),
        cwd=Path.cwd().resolve(),
        executable=sys.executable,
        source_root=editable_install.current_interpreter_source_root(),
        editable_roots=(Path.home() / "Meiva",),
    )

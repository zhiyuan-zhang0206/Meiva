# meiva:note
# The libraries the built-in skill scripts run: the `skill-tools` dependency group in
# pyproject.toml (trafilatura, feedparser, and the `yt-dlp` program). A skill script asks here
# instead of letting an ImportError or a missing program surface bare, so an environment built
# without the group fails with the group's name and the install command: install the group, or
# run the skill on a host that has it.
#
# This module is the pure half. A library import stays in the function that needs it and a
# program is looked up by the script that runs it, so an effect never enters the value modules
# beside it (the checker's `impure-edge` on a strict module).
# meiva:link docs/conventions/dev-setup.md
from __future__ import annotations

# meiva:note
# The group that carries these libraries. The message names it because installing it is the
# fix, and tests/contracts/test_skill_tools_group.py keeps the group and this name in step.
GROUP = "skill-tools"


# meiva:note
# A skill script needs a library or a program this environment does not have.
class MissingSkillToolError(RuntimeError):
    pass


# meiva:note
# The error a call site raises: which skill, what it needed, and the command that installs the
# group it comes from. Raising it beside the use keeps the failure where the capability is
# needed instead of at import time.
def missing(skill: str, needs: str) -> MissingSkillToolError:
    return MissingSkillToolError(
        f"{skill} needs {needs}, which comes from the `{GROUP}` dependency group, and this "
        f"environment does not have it. Install the group with `uv sync --group {GROUP}`, or "
        "run this skill on a host that has it"
    )

# meiva:note
# The host evidence a stop reads before it plans: is this a proven pre-application attempt.

from __future__ import annotations

from pathlib import Path

from base.deploy.lifecycle import start_serving
from base.paths import root_manifests_path
from cli.commands.lifecycle.root_driver import require_root_absent
from cli.commands.lifecycle.stop.shell_sessions import require_no_shell_sessions
from cli.start_identity import read_intent


# meiva:note
# Positive first-start evidence that no application could have admitted work.
#
# False when the home has progressed past initialization (or is a joined runner, which may
# refer to already-existing external work): the ordinary drain applies. Raises when the
# initialization journal contradicts what is on disk.
def require_unstarted_initialization(home: Path) -> bool:
    intent = read_intent(home)
    if intent is None or intent["phase"] != "configured":
        return False
    if "gateway" not in intent["roles"]:
        # meiva:note
        # A joined runner may refer to already-existing external work.
        return False
    # meiva:note
    # `start_serving.state_path()` takes no home: it resolves the process environment's home,
    # while the manifests path below is `home`'s. They name one home because a stop acts on the
    # home it runs in; the serving state taking `home` is a base change (`start_serving.*(home)`).
    if start_serving.state_path().exists() or root_manifests_path(home).exists():
        raise RuntimeError("initialization journal conflicts with application launch evidence")
    require_root_absent(home)
    require_no_shell_sessions()
    return True

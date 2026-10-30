# meiva:note
# A pty session child must never inherit a credential (task #5864, 405 ruling 2026-10-30): the
# session environment is the pty-sessions service's own plus the caller's overlay, so a secret the
# service carries would reach every shell on the machine.
from __future__ import annotations

from base.host.env.registry import session_credential_keys
from services.agent_runner.pty_sessions.service import child_env


# meiva:note
# The reverse assertion the ruling asks for: name every credential, not a sample - a set that
# grows later fails here until the new key is dropped too.
def test_child_env_drops_every_credential() -> None:
    base = dict.fromkeys(session_credential_keys(), "secret")
    base["MEIVA_HOME"] = "/home/u/.meiva"
    env = child_env({}, base_environ=base)
    assert [key for key in session_credential_keys() if key in env] == []


# meiva:note
# The positive control: a drop that also emptied the shell would pass the reverse assertion and
# break every session, so the floor a shell needs is asserted in the same breath.
def test_child_env_keeps_what_a_shell_needs() -> None:
    base = {"PATH": "/venv/bin:/usr/bin", "HOME": "/home/u", "MEIVA_HOME": "/home/u/.meiva"}
    env = child_env({}, base_environ=base)
    assert env["PATH"] == "/venv/bin:/usr/bin"
    assert env["HOME"] == "/home/u"
    assert env["MEIVA_HOME"] == "/home/u/.meiva"
    assert env["TERM"] == "xterm-256color"
    assert env["LANG"] == "en_US.UTF-8"


# meiva:note
# A credential is dropped even when the overlay names it: the rule is that a credential is read
# from the file that holds it, never carried in a child's environment. Silently forwarding it
# because a caller asked would make the rule a default rather than a boundary.
def test_child_env_drops_a_credential_the_overlay_names() -> None:
    env = child_env({"MEIVA_CLUSTER_SECRET": "asked-for"}, base_environ={})
    assert "MEIVA_CLUSTER_SECRET" not in env


# meiva:note
# Everything else in the overlay still wins over the base - the drop is a named set, not a
# narrowing of the overlay mechanism.
def test_child_env_applies_an_ordinary_overlay_entry() -> None:
    env = child_env({"MEIVA_SCHEDULE_ID": "49"}, base_environ={"MEIVA_SCHEDULE_ID": "1"})
    assert env["MEIVA_SCHEDULE_ID"] == "49"

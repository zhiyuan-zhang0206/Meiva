# meiva:note
# What bringing up the permissions helper decides, as functions of values.
#
# `lifecycle.py` reads the facts (is the job loaded, did the plist change, what did the helper
# answer to a ping) and performs the effects (write the plist, `launchctl bootstrap`, ping).
# Every judgment between them is here: whether an activation may proceed or must refuse, what a
# ping answer means, what the LaunchAgent plist contains, which plists race our socket, and
# whether a socket path fits macOS.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

__all__ = [
    "HELPER_PING_ATTEMPTS",
    "HELPER_PING_SETTLE_S",
    "Activation",
    "PingVerdict",
    "activation_plan",
    "helper_plist",
    "is_foreign_label_job",
    "ping_verdict",
    "socket_path_too_long",
]

# meiva:note
# A freshly bootstrapped helper needs a moment before its socket answers: ping this many times,
# this many seconds apart, before calling it unresponsive.
HELPER_PING_ATTEMPTS = 10
HELPER_PING_SETTLE_S = 0.5

# meiva:note
# Darwin `sockaddr_un.sun_path` has 104 bytes including the terminating NUL.
_MAX_SOCKET_PATH_BYTES = 103


# meiva:note
# What an activation does: refuse with `refusal`, or (no refusal) bootstrap the job when
# `bootstrap` is set and otherwise only observe the loaded one.
@dataclass(frozen=True)
class Activation:
    refusal: str | None
    bootstrap: bool


# meiva:note
# A loaded helper is only observed: replacing it needs an external stop and exact-home
# unregister first, so a descendant cannot unload its parent. An unloaded one is bootstrapped.
def activation_plan(
    *, loaded: bool, stop_intent: bool, plist_changed: bool, rebuilt: bool
) -> Activation:
    if loaded and stop_intent:
        return Activation(
            refusal="helper retirement is incomplete; finish meiva stop externally before starting",
            bootstrap=False,
        )
    if loaded and (plist_changed or rebuilt):
        return Activation(
            refusal=(
                "loaded helper differs from the requested artifact or job; stop and unregister "
                "this home's helper from outside its process tree before activating the replacement"
            ),
            bootstrap=False,
        )
    return Activation(refusal=None, bootstrap=not loaded)


PingVerdict = Literal["answered", "no_answer", "needs_upgrade"]


# meiva:note
# What one ping answer means. `None` is no answer (the call failed). A helper that answers but
# lacks the durable stop/shutdown protocol cannot be retired safely and needs a new artifact.
def ping_verdict(reply: Mapping[str, object] | None) -> PingVerdict:
    if reply is None or reply.get("pong") is not True:
        return "no_answer"
    if reply.get("root_stop_intent_v1") is not True or reply.get("helper_shutdown_v1") is not True:
        return "needs_upgrade"
    return "answered"


# meiva:note
# The LaunchAgent plist of this home's helper job.
def helper_plist(
    *, label: str, executable: str, log: str, socket: str, root_seed: str
) -> dict[str, object]:
    return {
        "Label": label,
        "ProgramArguments": [executable],
        "EnvironmentVariables": {
            "MEIVA_PERMISSIONS_HELPER_SOCKET": socket,
            "MEIVA_PERMISSIONS_HELPER_ROOT_SEED": root_seed,
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


# meiva:note
# Whether a parsed LaunchAgent plist is a job under a label other than `label` that pins this
# cluster's `socket`: it races our own job for the same socket. Other clusters' jobs pin their
# own sockets and are not foreign.
def is_foreign_label_job(plist: Mapping[str, object], *, label: str, socket: str) -> bool:
    if plist.get("Label") == label:
        return False
    env = plist.get("EnvironmentVariables")
    if not isinstance(env, dict):
        return False
    variables = cast("dict[str, object]", env)
    return variables.get("MEIVA_PERMISSIONS_HELPER_SOCKET") == socket


# meiva:note
# Whether an IPC socket path (as the bytes the OS sees) is too long for macOS.
def socket_path_too_long(path_bytes: bytes) -> bool:
    return len(path_bytes) > _MAX_SOCKET_PATH_BYTES

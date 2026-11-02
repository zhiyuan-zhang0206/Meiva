# meiva:note
# `meiva auth set-password` — thin client over the gateway's `PUT /api/auth/password`.
#
# The gateway owns every rule (the minimum length, the salted hash, which sessions end); this command
# reads the new password from one of the two places the operator names and forwards it. The password
# never travels in argv: `--prompt` asks twice at a no-echo prompt, `--stdin` reads the first line of
# standard input. The request presents the home's human cluster secret as its bearer (a machine API
# token would be refused: it cannot choose what a browser logs in with), so the command works only
# where that secret is held: the gateway home.

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO

import httpx
from pydantic import BaseModel, ValidationError

from base.cluster.machine import GatewayAccess

__all__ = [
    "PasswordInputError",
    "cmd_auth_set_password",
    "read_password_from_prompt",
    "read_password_from_stdin",
]

_TIMEOUT_S = 30.0


# meiva:note
# The password cannot be read as the operator asked: nothing was sent.
class PasswordInputError(ValueError):
    pass


# meiva:note
# The first line of `stream` without its line ending. Spaces are part of a password, so only the
# line ending goes.
def read_password_from_stdin(stream: TextIO) -> str:
    password = stream.readline().rstrip("\r\n")
    if not password:
        raise PasswordInputError("standard input held no password (its first line is empty)")
    return password


# meiva:note
# The password typed twice at `ask`, a no-echo prompt that takes its question and returns the answer.
def read_password_from_prompt(ask: Callable[[str], str]) -> str:
    password = ask("New web login password: ")
    if ask("Repeat the new password: ") != password:
        raise PasswordInputError("the two entries differ")
    return password


# meiva:note
# Send `password` to the gateway as the new web login password; the exit status.
#
# 0 when the gateway stored it, 1 when it refused it (its reason is printed) or could not be
# reached, or when this process holds no human cluster secret to ask with.
def cmd_auth_set_password(access: GatewayAccess, password: str) -> int:
    from base.cluster.machine import gateway_api_base
    from base.host.net.auth import bearer_header
    from base.host.net.http_dial import put as dial_put

    if not access.cluster_secret:
        print(
            "meiva auth set-password: this process holds no cluster secret to ask the gateway "
            "with; run it on the gateway's own home (its .env holds MEIVA_CLUSTER_SECRET)",
            file=sys.stderr,
        )
        return 1
    try:
        response = dial_put(
            f"{gateway_api_base(access.gateway_url)}/api/auth/password",
            headers=bearer_header(access.cluster_secret),
            json={"password": password},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        print(f"meiva auth set-password: the gateway is unreachable: {exc}", file=sys.stderr)
        return 1
    if not response.is_success:
        print(
            f"meiva auth set-password: the gateway refused it (HTTP {response.status_code}): "
            f"{_reason(response)}",
            file=sys.stderr,
        )
        return 1
    print(
        "web login password set; browser sessions the previous password opened are signed out "
        "(sessions a one-time login link opened stay)"
    )
    return 0


# meiva:note
# The gateway's typed error envelope, read only for the reason it gives.
class _Refusal(BaseModel):
    detail: str = ""


def _reason(response: httpx.Response) -> str:
    try:
        return _Refusal.model_validate_json(response.content).detail or response.text[:200]
    except ValidationError:
        return response.text[:200]

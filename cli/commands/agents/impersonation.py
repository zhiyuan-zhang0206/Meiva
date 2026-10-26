# meiva:note
# Cluster-local client for trusted named impersonation sessions.
#
# Only request hands out the scoped relay credential (claude prints it; dsh writes it to a
# private stub). Controller commands run under the session id with the lease's same-machine
# placement: no controller credential is minted, delivered, or stored. Session history is
# retained by the shared service.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO, assert_never
from uuid import UUID

from base.agents.context.clients import DatabaseFactory
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from cli.commands.agents.cleanup_after import run_then_clean_up
from cli.commands.agents.impersonation_adapters import codex_endpoint
from cli.commands.agents.relay.request_plan import (
    Handoff,
    RequestPlan,
    handoff_hint,
    plan_request,
    relay_stub_text,
)


# meiva:note
# The variables an impersonation command takes from the shell it runs in, read once by the
# handler: the relay credential a controller session exports, the credential stub path a dsh
# session's plugin exports, the conversation id the Antigravity CLI injects into the commands
# it runs, and the home `codex` resolves its daemon socket from. None of them is cluster
# configuration.
@dataclass(frozen=True)
class ImpersonationEnv:
    relay_token: str | None
    dsh_relay_stub: str | None
    antigravity_conversation: str | None
    codex_home: str | None

    # meiva:note
    # An empty variable is an absent one, as the shells that export these set them to "".
    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ImpersonationEnv:
        return cls(
            relay_token=environ.get("MEIVA_IMPERSONATION_RELAY_TOKEN") or None,
            dsh_relay_stub=environ.get("DSH_MEIVA_RELAY_STUB") or None,
            antigravity_conversation=(environ.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip()
            or None,
            codex_home=environ.get("CODEX_HOME") or None,
        )


# meiva:note
# The scoped relay credential the controller received at request time, from the channel the
# operator chose: the first stdin line when the accepting runtime pipes it (the native spawn
# handoff keeps the credential out of argv, the environment and any file), else the
# environment.
def relay_token(*, token_stdin: bool, stdin: TextIO, env: ImpersonationEnv) -> str:
    if token_stdin:
        line = stdin.readline()
        if not line:
            raise ValueError("no relay token on stdin (use --token-stdin with a piped credential)")
        return line.rstrip("\r\n")
    if env.relay_token is None:
        raise ValueError(
            "set MEIVA_IMPERSONATION_RELAY_TOKEN to the request's relay token "
            "(printed once in the request response)"
        )
    return env.relay_token


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, default=_json_value))


# meiva:note
# Write the resident-relay credential stub the session plugin consumes once.
#
# The file is created 0600 and `fchmod` defeats the umask, so the credential is never briefly
# world-readable between create and write. A failed write removes the partial file.
def write_relay_stub(
    path: Path, *, agent_id: int, session_id: int, token: str, interpreter: str | None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = relay_stub_text(
        agent_id=agent_id, session_id=session_id, token=token, interpreter=interpreter
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(text)
    except BaseException:
        if fd != -1:
            os.close(fd)
            fd = -1
        path.unlink(missing_ok=True)
        raise
    finally:
        if fd != -1:
            os.close(fd)


async def _wait_inbox(
    db: Database,
    lease_id: str,
    limit: int,
    wait: float,
    *,
    local_machine: str,
    redis_url: str,
) -> list[dict[str, Any]]:
    from base.agents import impersonation as control

    if not math.isfinite(wait) or wait < 0:
        raise ValueError("--wait must be finite and nonnegative")
    lease = await asyncio.to_thread(
        control.require_active, db, lease_id, local_machine=local_machine
    )
    listener = RedisInboundListener(redis_url, lease["agent_id"])
    loop = asyncio.get_running_loop()

    async def read_until_a_message_or_the_deadline() -> list[dict[str, Any]]:
        if wait:
            await listener.ensure_listening()
        deadline = loop.time() + wait
        while True:
            messages = await asyncio.to_thread(
                control.inbox, db, lease_id, limit=limit, local_machine=local_machine
            )
            remaining = deadline - loop.time()
            if messages or remaining <= 0:
                return messages
            await listener.wait_one(min(remaining, 30.0))

    return await run_then_clean_up(
        read_until_a_message_or_the_deadline(), listener.stop(), what="Redis listener stop"
    )


def _run_local(args: argparse.Namespace) -> int:
    import meiva

    code = (
        args.runtime.terminal.stdin.read()
        if args.file is None or args.file == "-"
        else Path(args.file).read_text(encoding="utf-8")
    )
    if not code.strip():
        raise ValueError("Python input must be nonempty")
    with meiva.impersonation.attach(args.session_id, agent_id=args.agent_id):
        exec(
            compile(code, args.file or "<meiva-external>", "exec"),
            {"__name__": "__main__", "meiva": meiva},
        )
    return 0


# meiva:note
# `impersonate send`: one message to another agent as the leased identity.
def _send(args: argparse.Namespace, *, database_factory: DatabaseFactory) -> int:
    from base.agents import impersonation as control
    from base.agents.impersonation import sessions
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.agents.control import send_agent_message

    content = args.runtime.terminal.stdin.read() if args.content == "-" else args.content
    db = database_factory()
    lease_id = sessions.private_id(
        db, args.agent_id, args.session_id, local_machine=args.runtime.machine.name()
    )
    lease = control.require_active(db, lease_id, local_machine=args.runtime.machine.name())
    source = f"agent:{lease['agent_id']}"
    status = send_agent_message(
        gateway_access_from_boot(args.runtime.config),
        args.target_agent_id,
        content,
        source=source,
    )
    _emit({"status": status, "to": args.target_agent_id, "source": source})
    return 0


# meiva:note
# Recorded before the lease exists: the accepting runtime and the ended-lease notice both
# run outside this process's environment, and the Antigravity CLI injects its endpoint only
# into the commands it runs, so this request is the one place that can learn it. The record
# is owner-only; its CSRF token never reaches argv, the environment, the database or a log
# line.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def _record_antigravity_endpoint(plan: RequestPlan, home: Path) -> None:
    from base.agents.impersonation.host_transport import (
        resolve_antigravity_endpoint,
        write_antigravity_endpoint,
    )

    if plan.thread_id is None:
        raise ValueError("the antigravity relay needs a conversation id")
    write_antigravity_endpoint(
        resolve_antigravity_endpoint(plan.thread_id, home=home),
        thread_id=plan.thread_id,
        home=home,
    )


# meiva:note
# `impersonate request`: create the lease, then hand its relay the credential.
def _request(
    args: argparse.Namespace, *, database_factory: DatabaseFactory, env: ImpersonationEnv
) -> int:
    from base.agents.impersonation import sessions
    from base.config.service_read import ConfigAuthority
    from base.native_process.ownership import process_metadata

    plan = plan_request(
        args.relay_provider,
        args.relay_thread_id,
        args.relay_codex_remote,
        conversation_env=env.antigravity_conversation,
        dsh_stub_env=env.dsh_relay_stub,
    )
    home: Path = args.runtime.home
    endpoint = plan.codex_remote
    match plan.handoff:
        case Handoff.CODEX:
            endpoint = codex_endpoint(endpoint, codex_home=env.codex_home)
        case Handoff.ANTIGRAVITY:
            _record_antigravity_endpoint(plan, home)
        case Handoff.CLAUDE | Handoff.DSH:
            pass
        case _:
            assert_never(plan.handoff)
    config = args.runtime.config
    runtime = config.ensure_eager()
    response = sessions.request(
        database_factory(),
        EventBus.from_settings(),
        args.agent_id,
        authority=ConfigAuthority(
            runtime=runtime,
            all_domains=runtime if runtime.profile is None else config.complete_read_model(),
            env_path=home / ".env",
        ),
        name=args.name,
        executor_name=args.caller,
        process_metadata={
            **process_metadata(codex_home=env.codex_home),
            "invoked_python": sys.executable,
        },
        ttl_seconds=args.ttl,
        reason=args.reason,
        provider=args.relay_provider,
        thread_id=plan.thread_id,
        codex_remote=endpoint,
        batch_window_seconds=args.relay_batch_window_seconds,
        local_machine=args.runtime.machine.name(),
    )
    if plan.dsh_stub is not None:
        write_relay_stub(
            Path(plan.dsh_stub),
            agent_id=args.agent_id,
            session_id=int(response["session_id"]),
            token=str(response.pop("relay_token")),
            interpreter=sys.executable,
        )
    _emit(response)
    print(handoff_hint(plan), file=sys.stderr)
    return 0


def _dispatch(
    args: argparse.Namespace, *, database_factory: DatabaseFactory, env: ImpersonationEnv
) -> int:
    from base.agents import impersonation as control
    from base.agents.impersonation import sessions
    from base.agents.impersonation.history import public_session, say

    command = args.impersonation_cmd
    if command == "request":
        return _request(args, database_factory=database_factory, env=env)
    db = database_factory()
    bus = EventBus.from_settings()
    if command == "list":
        _emit(sessions.list_sessions(db, args.agent_id, before=args.before, limit=args.limit))
        return 0
    if command == "send":
        return _send(args, database_factory=database_factory)
    args.lease_id = sessions.private_id(
        db, args.agent_id, args.session_id, local_machine=args.runtime.machine.name()
    )
    if command == "status":
        _emit(
            public_session(
                control.get(db, bus, args.lease_id, local_machine=args.runtime.machine.name())
            )
        )
    elif command == "renew":
        _emit(
            public_session(
                control.renew(
                    db,
                    bus,
                    args.lease_id,
                    ttl_seconds=args.ttl,
                    local_machine=args.runtime.machine.name(),
                )
            )
        )
    elif command == "release":
        summary = args.runtime.terminal.stdin.read() if args.summary == "-" else args.summary
        _emit(
            public_session(
                control.release(
                    db,
                    bus,
                    args.lease_id,
                    summary,
                    local_machine=args.runtime.machine.name(),
                )
            )
        )
    elif command == "inbox":
        _emit(
            asyncio.run(
                _wait_inbox(
                    db,
                    args.lease_id,
                    args.limit,
                    args.wait,
                    local_machine=args.runtime.machine.name(),
                    redis_url=args.runtime.config.view.data_plane.redis_url,
                )
            )
        )
    elif command == "ack":
        control.ack(
            db,
            bus,
            args.lease_id,
            args.message_ids,
            local_machine=args.runtime.machine.name(),
        )
        _emit({"acknowledged": args.message_ids})
    elif command == "say":
        content = args.runtime.terminal.stdin.read() if args.content == "-" else args.content
        _emit(
            {
                "seq": say(
                    db,
                    bus,
                    args.lease_id,
                    content,
                    phase=args.phase,
                    message_key=args.key,
                    local_machine=args.runtime.machine.name(),
                )
            }
        )
    elif command == "exec":
        return _run_local(args)
    else:
        raise ValueError(f"unknown impersonation command: {command}")
    return 0


# meiva:note
# Run one command, with operational errors confined to stderr.
def cmd_impersonate(
    args: argparse.Namespace, *, database_factory: DatabaseFactory, env: ImpersonationEnv
) -> int:
    try:
        return _dispatch(args, database_factory=database_factory, env=env)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"impersonation: {exc}", file=sys.stderr)
        return 1

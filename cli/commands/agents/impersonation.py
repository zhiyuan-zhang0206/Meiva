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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TextIO, assert_never
from uuid import UUID

from base.agents.context.clients import DatabaseFactory
from base.agents.impersonation import InboxMessage
from base.agents.impersonation.host_transport import (
    AntigravityServer,
    antigravity_server_from_environment,
)
from base.clock import Timers
from base.db import Database
from base.events.live.bus import EventBus, event_bus_config_from_config
from base.events.live.redis_listener import RedisInboundListener
from base.host.boundary.fields import int_field, str_field
from base.host.boundary.options import options
from cli.commands.agents.cleanup_after import run_then_clean_up
from cli.commands.agents.impersonation_adapters import codex_endpoint
from cli.commands.agents.relay.request_plan import (
    Handoff,
    RequestPlan,
    handoff_hint,
    plan_request,
    relay_stub_text,
)
from cli.database.runtime import runtime_of


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
    events_channel: str,
    timers: Timers,
) -> list[InboxMessage]:
    from base.agents import impersonation as control

    if not math.isfinite(wait) or wait < 0:
        raise ValueError("--wait must be finite and nonnegative")
    agent_id = int_field(
        await asyncio.to_thread(control.require_active, db, lease_id, local_machine=local_machine),
        "agent_id",
    )
    listener = RedisInboundListener(
        redis_url, agent_id, events_channel=events_channel, timers=timers
    )

    async def read_until_a_message_or_the_deadline() -> list[InboxMessage]:
        if wait:
            await listener.ensure_listening()
        deadline = timers.monotonic() + wait
        while True:
            messages = await asyncio.to_thread(
                control.inbox, db, lease_id, limit=limit, local_machine=local_machine
            )
            remaining = deadline - timers.monotonic()
            if messages or remaining <= 0:
                return messages
            await listener.wait_one(min(remaining, 30.0))

    return await run_then_clean_up(
        read_until_a_message_or_the_deadline(), listener.stop(), what="Redis listener stop"
    )


@dataclass(frozen=True)
class _SessionTarget:
    agent_id: int
    session_id: int


@dataclass(frozen=True)
class _ExecOptions:
    file: str | None
    agent_id: int
    session_id: int


def _run_local(args: argparse.Namespace) -> int:
    import meiva

    opts = options(args, _ExecOptions)
    code = (
        runtime_of(args).terminal.stdin.read()
        if opts.file is None or opts.file == "-"
        else Path(opts.file).read_text(encoding="utf-8")
    )
    if not code.strip():
        raise ValueError("Python input must be nonempty")
    with meiva.impersonation.attach(opts.session_id, agent_id=opts.agent_id):
        exec(
            compile(code, opts.file or "<meiva-external>", "exec"),
            {"__name__": "__main__", "meiva": meiva},
        )
    return 0


@dataclass(frozen=True)
class _SendOptions:
    agent_id: int
    session_id: int
    target_agent_id: int
    content: str


# meiva:note
# `impersonate send`: one message to another agent as the leased identity.
def _send(args: argparse.Namespace, *, database_factory: DatabaseFactory) -> int:
    from base.agents import impersonation as control
    from base.agents.impersonation import sessions
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.agents.control import send_agent_message

    opts = options(args, _SendOptions)
    runtime = runtime_of(args)
    content = runtime.terminal.stdin.read() if opts.content == "-" else opts.content
    db = database_factory()
    lease_id = sessions.private_id(
        db, opts.agent_id, opts.session_id, local_machine=runtime.machine.name()
    )
    lease = control.require_active(db, lease_id, local_machine=runtime.machine.name())
    source = f"agent:{lease['agent_id']}"
    receipt = send_agent_message(
        gateway_access_from_boot(runtime.config),
        opts.target_agent_id,
        content,
        source=source,
        idempotency_key=runtime.new_id(),
    )
    _emit({"status": receipt.status, "to": opts.target_agent_id, "source": source})
    return 0


# meiva:note
# Recorded before the lease exists: the accepting runtime and the ended-lease notice both
# run outside this process's environment, and the Antigravity CLI injects its endpoint only
# into the commands it runs, so this request is the one place that can learn it. The record
# is owner-only; its CSRF token never reaches argv, the environment, the database or a log
# line.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def _record_antigravity_endpoint(
    plan: RequestPlan, home: Path, *, antigravity_server: AntigravityServer | None
) -> None:
    from base.agents.impersonation.host_transport import (
        resolve_antigravity_endpoint,
        write_antigravity_endpoint,
    )

    if plan.thread_id is None:
        raise ValueError("the antigravity relay needs a conversation id")
    write_antigravity_endpoint(
        resolve_antigravity_endpoint(
            plan.thread_id, home=home, antigravity_server=antigravity_server
        ),
        thread_id=plan.thread_id,
        home=home,
    )


@dataclass(frozen=True)
class _RequestOptions:
    agent_id: int
    caller: str
    name: str
    ttl: int
    reason: str
    relay_provider: Literal["codex", "claude", "dsh", "antigravity"]
    relay_thread_id: str | None
    relay_codex_remote: str | None
    relay_batch_window_seconds: int


# meiva:note
# `impersonate request`: create the lease, then hand its relay the credential.
def _request(
    args: argparse.Namespace, *, database_factory: DatabaseFactory, env: ImpersonationEnv
) -> int:
    from base.agents.impersonation import sessions
    from base.config.service_read import ConfigAuthority
    from base.native_process.ownership import process_metadata

    opts = options(args, _RequestOptions)
    runtime = runtime_of(args)
    plan = plan_request(
        opts.relay_provider,
        opts.relay_thread_id,
        opts.relay_codex_remote,
        conversation_env=env.antigravity_conversation,
        dsh_stub_env=env.dsh_relay_stub,
    )
    home: Path = runtime.home
    endpoint = plan.codex_remote
    match plan.handoff:
        case Handoff.CODEX:
            endpoint = codex_endpoint(
                endpoint, codex_home=env.codex_home, user_home=runtime.session.user_home
            )
        case Handoff.ANTIGRAVITY:
            _record_antigravity_endpoint(
                plan,
                home,
                antigravity_server=antigravity_server_from_environment(
                    runtime.config.process_environment()
                ),
            )
        case Handoff.CLAUDE | Handoff.DSH:
            pass
        case _:
            assert_never(plan.handoff)
    config = runtime.config
    eager = config.ensure_eager()
    response = sessions.request(
        database_factory(),
        EventBus(event_bus_config_from_config(config), rng=runtime.rng, timers=runtime.timers),
        opts.agent_id,
        authority=ConfigAuthority(
            runtime=eager,
            all_domains=eager if eager.profile is None else config.complete_read_model(),
            env_path=home / ".env",
        ),
        name=opts.name,
        executor_name=opts.caller,
        process_metadata={
            **process_metadata(pid=runtime.pid, codex_home=env.codex_home),
            # meiva:note
            # The interpreter this verb runs on, read from the runtime's host: `Host.system()` is
            # where this process read its own platform once, and the lease records that value for
            # whoever reads the metadata back.
            "invoked_python": runtime.host.platform.executable,
        },
        ttl_seconds=opts.ttl,
        reason=opts.reason,
        provider=opts.relay_provider,
        thread_id=plan.thread_id,
        codex_remote=endpoint,
        batch_window_seconds=opts.relay_batch_window_seconds,
        local_machine=runtime.machine.name(),
    )
    if plan.dsh_stub is not None:
        write_relay_stub(
            Path(plan.dsh_stub),
            agent_id=opts.agent_id,
            session_id=int_field(response, "session_id"),
            token=str_field(response, "relay_token"),
            interpreter=runtime.host.platform.executable,
        )
        del response["relay_token"]
    _emit(response)
    print(handoff_hint(plan), file=sys.stderr)
    return 0


@dataclass(frozen=True)
class _ListOptions:
    agent_id: int
    before: int | None
    limit: int


@dataclass(frozen=True)
class _RenewOptions:
    ttl: int


@dataclass(frozen=True)
class _ReleaseOptions:
    summary: str


@dataclass(frozen=True)
class _InboxOptions:
    limit: int
    wait: float


@dataclass(frozen=True)
class _AckOptions:
    message_ids: Sequence[int]


@dataclass(frozen=True)
class _SayOptions:
    content: str
    key: str
    phase: Literal["commentary", "final"]


@dataclass(frozen=True)
class _Command:
    impersonation_cmd: str


def _dispatch(
    args: argparse.Namespace, *, database_factory: DatabaseFactory, env: ImpersonationEnv
) -> int:
    from base.agents import impersonation as control
    from base.agents.impersonation import sessions
    from base.agents.impersonation.history import public_session, say

    command = options(args, _Command).impersonation_cmd
    if command == "request":
        return _request(args, database_factory=database_factory, env=env)
    runtime = runtime_of(args)
    db = database_factory()
    bus = EventBus(
        event_bus_config_from_config(runtime.config),
        rng=runtime.rng,
        timers=runtime.timers,
    )
    if command == "list":
        listing = options(args, _ListOptions)
        _emit(
            sessions.list_sessions(db, listing.agent_id, before=listing.before, limit=listing.limit)
        )
        return 0
    if command == "send":
        return _send(args, database_factory=database_factory)
    target = options(args, _SessionTarget)
    lease_id = sessions.private_id(
        db, target.agent_id, target.session_id, local_machine=runtime.machine.name()
    )
    if command == "status":
        _emit(public_session(control.get(db, bus, lease_id, local_machine=runtime.machine.name())))
    elif command == "renew":
        _emit(
            public_session(
                control.renew(
                    db,
                    bus,
                    lease_id,
                    ttl_seconds=options(args, _RenewOptions).ttl,
                    local_machine=runtime.machine.name(),
                )
            )
        )
    elif command == "release":
        summary = options(args, _ReleaseOptions).summary
        _emit(
            public_session(
                control.release(
                    db,
                    bus,
                    lease_id,
                    runtime.terminal.stdin.read() if summary == "-" else summary,
                    local_machine=runtime.machine.name(),
                )
            )
        )
    elif command == "inbox":
        inbox = options(args, _InboxOptions)
        _emit(
            asyncio.run(
                _wait_inbox(
                    db,
                    lease_id,
                    inbox.limit,
                    inbox.wait,
                    local_machine=runtime.machine.name(),
                    redis_url=runtime.config.view.data_plane.redis_url,
                    events_channel=bus.channel,
                    timers=runtime.timers,
                )
            )
        )
    elif command == "ack":
        acknowledged = list(options(args, _AckOptions).message_ids)
        control.ack(db, bus, lease_id, acknowledged, local_machine=runtime.machine.name())
        _emit({"acknowledged": acknowledged})
    elif command == "say":
        spoken = options(args, _SayOptions)
        content = runtime.terminal.stdin.read() if spoken.content == "-" else spoken.content
        _emit(
            {
                "seq": say(
                    db,
                    bus,
                    lease_id,
                    content,
                    phase=spoken.phase,
                    message_key=spoken.key,
                    local_machine=runtime.machine.name(),
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

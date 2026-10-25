#!/usr/bin/env python3
"""Rotate the independent Redis data-plane credentials on a gateway host.

Dry-run is the default. ``--scope admin`` rotates the Redis ``default`` /
requirepass password; ``--scope runner`` rotates the Redis runtime ACL
password; the default rotates both. It never changes ``MEIVA_CLUSTER_SECRET``:
that is the separate, emergency-only control-plane bearer rotation.

PostgreSQL has no rotatable password here: the schema owner is NOLOGIN, the
administrator is the OS user over the owner-only socket, and application
logins are the home's one write generation (``base.cluster.authority``), which
nothing rotates. Redis credentials do not rotate per rollout; this script is
the explicit operator action.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self, TypeGuard

import redis
from dotenv import dotenv_values
from redis.exceptions import RedisError

from base.cluster import (
    ClusterRecord,
    ensure_cluster_redis_acl,
    get_record,
    redis_identity,
)
from base.cluster.derive import REDIS_PASSWORD_ENV
from base.config import settings
from base.host.env.dotenv_file import upsert_env
from base.host.net.url_secret import url_host, url_with_password
from base.paths import meiva_home
from ops.env_audit import EnvWriteAudit

_TOKEN_BYTES = 32
_GATEWAY_CONTEXT_ERROR = (
    "data-plane secret rotation must run on the gateway host in a gateway context, not from "
    "an agent shell. Run:\n"
    "cd <gateway checkout (e.g. ~/.meiva/source)> && unset MEIVA_PROCESS_PROFILE && "
    ".venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py ..."
)


class RotationScope(StrEnum):
    ADMIN = "admin"
    RUNNER = "runner"
    BOTH = "both"

    @classmethod
    def parse(cls, raw: str) -> Self:
        try:
            return cls(raw)
        except ValueError:
            raise ValueError("unsupported rotation scope") from None


class RotationPhase(StrEnum):
    MINTED = "minted"
    ADMIN = "admin"
    RUNNER = "runner"
    VERIFIED = "verified"
    ENV_WRITTEN = "env_written"

    @classmethod
    def parse(cls, raw: str) -> Self:
        try:
            return cls(raw)
        except ValueError:
            raise ValueError("unsupported rotation phase") from None


def _journal_text(record: dict[str, object], name: str) -> str:
    value = record[name]
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"invalid Redis rotation journal field: {name}")
    return value


def _journal_port(record: dict[str, object]) -> int:
    port = record["redis_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RuntimeError("invalid Redis rotation journal field: redis_port")
    return port


def _journal_stamp(record: dict[str, object]) -> str:
    stamp = _journal_text(record, "started_at")
    if "/" in stamp or "\0" in stamp:
        raise RuntimeError("invalid Redis rotation journal field: started_at")
    return stamp


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


@dataclass(frozen=True)
class GatewayContext:
    home: Path
    profile: str | None
    redis_url: str
    events_channel: str = "meiva:events"
    is_remote: bool = False


@dataclass(frozen=True)
class RedisProbeSpec:
    label: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class RunnerAclPlan:
    user: str
    redis_admin_url: str
    runtime_password: str = field(repr=False)
    channel_prefix: str


@dataclass
class RotationState:
    """All mutable values needed to resume safely. Kept in a 0600 file because
    it includes both the old and replacement Redis passwords."""

    scope: RotationScope
    old_redis_admin_password: str = field(repr=False)
    new_redis_admin_password: str = field(repr=False)
    old_redis_password: str = field(repr=False)
    new_redis_password: str = field(repr=False)
    redis_port: int
    redis_host: str
    redis_user: str
    started_at: str = "1970-01-01T00:00:00+00:00"
    phase: RotationPhase = RotationPhase.MINTED

    def path(self, home: Path) -> Path:
        stamp = self.started_at.replace(":", "").replace("+00:00", "Z")
        return home / "backups" / "secret-rotation" / f"data-plane-{stamp}.json"

    def save(self, home: Path) -> Path:
        path = self.path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        path.chmod(0o600)
        return path

    # meiva:note
    # Saved journals always contain every dataclass field. Defaults belong to a fresh
    # rotation, never to a truncated resume record. Validate before plan output or effects;
    # diagnostics name only canonical fields, since the record contains passwords.
    @classmethod
    def load(cls, path: Path) -> RotationState:
        try:
            raw: object = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeError("invalid Redis rotation journal JSON") from None
        record = raw if _is_str_object_dict(raw) else {}
        if set(record) != {item.name for item in fields(cls)}:
            raise RuntimeError(
                f"{path} is not a Redis-only rotation journal (Postgres credentials no "
                "longer rotate here); start a new rotation"
            )
        try:
            return cls(
                scope=RotationScope.parse(_journal_text(record, "scope")),
                old_redis_admin_password=_journal_text(record, "old_redis_admin_password"),
                new_redis_admin_password=_journal_text(record, "new_redis_admin_password"),
                old_redis_password=_journal_text(record, "old_redis_password"),
                new_redis_password=_journal_text(record, "new_redis_password"),
                redis_port=_journal_port(record),
                redis_host=_journal_text(record, "redis_host"),
                redis_user=_journal_text(record, "redis_user"),
                started_at=_journal_stamp(record),
                phase=RotationPhase.parse(_journal_text(record, "phase")),
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None

    @property
    def rotates_admin(self) -> bool:
        return self.scope in {RotationScope.ADMIN, RotationScope.BOTH}

    @property
    def rotates_runner(self) -> bool:
        return self.scope in {RotationScope.RUNNER, RotationScope.BOTH}

    @property
    def redis_admin_password(self) -> str:
        return (
            self.new_redis_admin_password if self.rotates_admin else self.old_redis_admin_password
        )

    @property
    def redis_password(self) -> str:
        return self.new_redis_password if self.rotates_runner else self.old_redis_password


def mint_secret() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _require_gateway_context(profile: str | None) -> None:
    if profile == "agent":
        raise RuntimeError(_GATEWAY_CONTEXT_ERROR)


def build_state(
    context: GatewayContext,
    record: ClusterRecord | None,
    scope: str = RotationScope.BOTH,
    *,
    started_at: str = "1970-01-01T00:00:00+00:00",
) -> RotationState:
    selected = RotationScope.parse(scope)
    _require_gateway_context(context.profile)
    if record is None:
        raise RuntimeError("no cluster record — cannot resolve data-plane ports")
    values = dotenv_values(context.home / ".env")
    old_admin = (values.get("MEIVA_REDIS_ADMIN_PASSWORD") or "").strip()
    old_runtime = (values.get(REDIS_PASSWORD_ENV) or "").strip()
    if not (old_admin and old_runtime):
        raise RuntimeError(
            "this home records no Redis credentials: it was born before Redis always "
            "authenticated and no conversion exists; re-birth it as a new home"
        )
    return RotationState(
        scope=selected,
        old_redis_admin_password=old_admin,
        new_redis_admin_password=mint_secret()
        if selected in {RotationScope.ADMIN, RotationScope.BOTH}
        else old_admin,
        old_redis_password=old_runtime,
        new_redis_password=mint_secret()
        if selected in {RotationScope.RUNNER, RotationScope.BOTH}
        else old_runtime,
        redis_port=record.ports["redis"],
        redis_host=url_host(context.redis_url),
        redis_user=redis_identity(context.redis_url),
        started_at=started_at,
    )


def redis_probe(host: str, port: int, password: str, *, username: str) -> bool:
    try:
        with redis.Redis(
            host=host,
            port=port,
            username=username,
            password=password,
            socket_connect_timeout=3,
            socket_timeout=3,
        ) as client:
            return bool(client.ping())
    except (RedisError, OSError):
        return False


def _probe_specs(state: RotationState, admin: str, runtime: str) -> list[RedisProbeSpec]:
    specs = [
        RedisProbeSpec(
            label="Redis default",
            host=state.redis_host,
            port=state.redis_port,
            username="default",
            password=admin,
        )
    ]
    if state.rotates_runner:
        specs.append(
            RedisProbeSpec(
                label="Redis ACL user",
                host=state.redis_host,
                port=state.redis_port,
                username=state.redis_user,
                password=runtime,
            )
        )
    return specs


def preflight_probes(state: RotationState) -> list[RedisProbeSpec]:
    return _probe_specs(state, state.old_redis_admin_password, state.old_redis_password)


def verify_probes(state: RotationState) -> list[RedisProbeSpec]:
    return _probe_specs(state, state.redis_admin_password, state.redis_password)


def _checks(specs: list[RedisProbeSpec]) -> list[tuple[str, bool]]:
    return [
        (spec.label, redis_probe(spec.host, spec.port, spec.password, username=spec.username))
        for spec in specs
    ]


def preflight(state: RotationState) -> bool:
    """Refuse to rotate over pre-existing credential drift."""
    checks = _checks(preflight_probes(state))
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
    return all(ok for _label, ok in checks)


def working_redis_admin_password(state: RotationState) -> str:
    for password in (state.new_redis_admin_password, state.old_redis_admin_password):
        if redis_probe(state.redis_host, state.redis_port, password, username="default"):
            return password
    raise RuntimeError("Redis default user rejects both recorded admin passwords")


def admin_requirepass_command(state: RotationState) -> tuple[str, ...]:
    if not state.rotates_admin:
        return ()
    return ("CONFIG", "SET", "requirepass", state.new_redis_admin_password)


def apply_admin(state: RotationState) -> None:
    """Rotate the Redis default user, keeping the runtime ACL user live."""
    command = admin_requirepass_command(state)
    if not command:
        return
    current = working_redis_admin_password(state)
    with redis.Redis(
        host=state.redis_host,
        port=state.redis_port,
        username="default",
        password=current,
        socket_connect_timeout=3,
        socket_timeout=3,
    ) as client:
        client.execute_command(*command)


def runner_acl_plan(
    state: RotationState,
    *,
    admin_password: str,
    events_channel: str = "meiva:events",
) -> RunnerAclPlan | None:
    if not state.rotates_runner:
        return None
    return RunnerAclPlan(
        user=state.redis_user,
        redis_admin_url=f"redis://default:{admin_password}@{state.redis_host}:{state.redis_port}",
        runtime_password=state.new_redis_password,
        channel_prefix=events_channel.removesuffix(":events"),
    )


def apply_runner(
    state: RotationState,
    *,
    events_channel: str = "meiva:events",
) -> None:
    """Rotate the Redis runtime ACL password."""
    if not state.rotates_runner:
        return
    admin_password = working_redis_admin_password(state)
    plan = runner_acl_plan(
        state,
        admin_password=admin_password,
        events_channel=events_channel,
    )
    if plan is None:
        return
    ensure_cluster_redis_acl(
        plan.user,
        redis_admin_url=plan.redis_admin_url,
        runtime_password=plan.runtime_password,
        channel_prefix=plan.channel_prefix,
    )


def verify(state: RotationState) -> None:
    failed = [label for label, ok in _checks(verify_probes(state)) if not ok]
    if failed:
        raise RuntimeError(f"rotation verification failed: {', '.join(failed)}")


def write_env(state: RotationState, context: GatewayContext) -> None:
    env_path = context.home / ".env"
    values = dotenv_values(env_path)
    redis_url = url_with_password(
        (values.get("MEIVA_REDIS_URL") or context.redis_url).strip(),
        state.redis_password,
    )
    upsert_env(
        env_path,
        {
            "MEIVA_REDIS_ADMIN_PASSWORD": state.redis_admin_password,
            REDIS_PASSWORD_ENV: state.redis_password,
            "MEIVA_REDIS_URL": redis_url,
        },
        audit=EnvWriteAudit(site="rotate_data_plane_secrets"),
    )


def _run_phase(
    state: RotationState,
    home: Path,
    phase: RotationPhase,
    fn: Callable[[RotationState], None],
) -> None:
    print(f"-> {phase}")
    fn(state)
    state.phase = phase
    state.save(home)
    print(f"   ✓ {phase}")


def print_plan(state: RotationState, *, dry_run: bool) -> None:
    print(f"scope:             {state.scope}")
    print(f"redis user:        {state.redis_user!r}")
    print(f"redis port:        {state.redis_port}")
    print(f"mode:              {'DRY RUN (read-only)' if dry_run else 'EXECUTE'}")
    if state.rotates_runner:
        print(
            "runner follow-up: refresh every enrolled runner after this rotation; "
            "cached runner Redis URLs keep the old password until they refetch."
        )


def rotation_phases(*, execute: bool) -> tuple[RotationPhase, ...]:
    if not execute:
        return ()
    return (
        RotationPhase.ADMIN,
        RotationPhase.RUNNER,
        RotationPhase.VERIFIED,
        RotationPhase.ENV_WRITTEN,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rotate the Redis data-plane credentials.")
    parser.add_argument(
        "--scope",
        choices=sorted(scope.value for scope in RotationScope),
        default=RotationScope.BOTH.value,
    )
    parser.add_argument("--execute", action="store_true", help="perform the rotation")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--resume", metavar="STATE_FILE", help="resume a saved rotation")
    return parser


def run_rotation(
    argv: list[str] | None,
    *,
    context: GatewayContext,
    record: ClusterRecord | None,
    started_at: str,
) -> int:
    if context.is_remote:
        print(
            "✗ this cluster's data plane is remote-managed — rotation is a "
            "local-instance operation (requirepass / ACL). A remote/SaaS plane "
            "rotates credentials at the provider; update MEIVA_REDIS_URL here.",
            file=sys.stderr,
        )
        return 1
    args = _build_parser().parse_args(argv)
    try:
        _require_gateway_context(context.profile)
    except RuntimeError:
        print(_GATEWAY_CONTEXT_ERROR, file=sys.stderr)
        return 1
    state = (
        RotationState.load(Path(args.resume))
        if args.resume
        else build_state(context, record, args.scope, started_at=started_at)
    )
    phases = rotation_phases(execute=args.execute)
    print_plan(state, dry_run=not phases)
    if not phases:
        preflight(state)
        print("\n[dry-run] no changes made.")
        return 0
    if not args.resume and not preflight(state):
        print("\n✗ refusing to rotate over pre-existing credential drift.", file=sys.stderr)
        return 1
    if not args.yes:
        answer = input(f"\nType 'rotate {state.scope}' to continue: ")
        if answer.strip() != f"rotate {state.scope}":
            print("aborted.")
            return 1

    try:
        _run_phase(state, context.home, RotationPhase.ADMIN, apply_admin)
        _run_phase(
            state,
            context.home,
            RotationPhase.RUNNER,
            lambda s: apply_runner(s, events_channel=context.events_channel),
        )
        _run_phase(state, context.home, RotationPhase.VERIFIED, verify)
        _run_phase(state, context.home, RotationPhase.ENV_WRITTEN, lambda s: write_env(s, context))
    except Exception as exc:
        state_path = state.save(context.home)
        print(f"\n✗ rotation failed at {state.phase.value!r}: {exc}", file=sys.stderr)
        print(f"  resume with --execute --resume {state_path}", file=sys.stderr)
        return 1

    print("\n✓ data-plane rotation complete.")
    if state.rotates_runner:
        print("NEXT: restart every enrolled runner so it refetches the runner Redis URL.")
    return 0


def main(argv: list[str] | None = None) -> int:
    if settings.data_plane.is_remote:
        print(
            "✗ this cluster's data plane is remote-managed — rotation is a "
            "local-instance operation (requirepass / ACL). A remote/SaaS plane "
            "rotates credentials at the provider; update MEIVA_REDIS_URL here.",
            file=sys.stderr,
        )
        return 1
    args = _build_parser().parse_args(argv)
    try:
        _require_gateway_context(settings.profile)
    except RuntimeError:
        print(_GATEWAY_CONTEXT_ERROR, file=sys.stderr)
        return 1
    if args.resume:
        RotationState.load(Path(args.resume))
    home = meiva_home()
    context = GatewayContext(
        home=home,
        profile=settings.profile,
        redis_url=settings.data_plane.redis_url,
        events_channel=settings.data_plane.events_channel,
        is_remote=False,
    )
    return run_rotation(
        argv,
        context=context,
        record=get_record(home),
        started_at=datetime.now(UTC).isoformat(),
    )


if __name__ == "__main__":
    raise SystemExit(main())

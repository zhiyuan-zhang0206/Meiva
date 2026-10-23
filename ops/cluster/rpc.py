from __future__ import annotations

import hashlib
import json
import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, Self

import httpx
from pydantic import ValidationError

from base.clock import SYSTEM_TIMERS, Timers
from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.cluster.machine.ambient import gateway_access_from_settings
from base.cluster.machines import (
    MachineGatewayUrlMissing,
    MachineNotRegistered,
)
from base.cluster.machines import (
    lookup as lookup_machine_url,
)
from base.config import settings
from base.db import Database
from ops.rpc_schemas import OpEnvelope, OpKind, OpResponse, OpStatus, is_op_kind

__all__ = [
    "ClusterOpFailed",
    "ClusterOpTargetAbsent",
    "ClusterOpUnreachable",
    "dispatch_to_machine",
    "dispatch_to_url",
]

_log = logging.getLogger(__name__)

_RETRY_BASE_DELAY_S = 0.5
_RETRY_MAX_DELAY_S = 4.0

NON_IDEMPOTENT_KINDS = frozenset(
    {
        "spawn-launch",
        "lifecycle",
    }
)
_BUSINESS_ID_KEYS = {"spawn-launch": "agent_id"}


def retry_delay_s(attempt: int, *, uniform: Callable[[float, float], float]) -> float:
    base = min(_RETRY_BASE_DELAY_S * (2**attempt), _RETRY_MAX_DELAY_S)
    return base * uniform(0.5, 1.5)


def worst_case_dispatch_seconds(timeout_s: float | None = None) -> float:
    config = rpc_config_from_settings()
    retries = max(config.max_retries, 0)
    backoff = sum(
        min(_RETRY_BASE_DELAY_S * (2**attempt), _RETRY_MAX_DELAY_S) for attempt in range(retries)
    )
    attempt_s = config.timeout_seconds if timeout_s is None else timeout_s
    return (retries + 1) * attempt_s + backoff * 1.5


def default_idempotency_key(target_machine: str, kind: OpKind, payload: dict[str, Any]) -> str:
    business_id_key = _BUSINESS_ID_KEYS.get(kind)
    business_id = payload.get(business_id_key) if business_id_key is not None else None
    if business_id is None:
        return f"{kind}:{uuid.uuid4()}"
    canonical_payload = json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)
    fingerprint = hashlib.sha256(canonical_payload.encode()).hexdigest()[:16]
    return f"{kind}:{target_machine}:{business_id}:{fingerprint}"


# meiva:link ops/docs/ops-module.md
class _RetryableFailure(Exception):  # noqa: N818
    def __init__(self, cause: Exception, *, url: str) -> None:
        super().__init__(f"{url}: {cause!r}")
        self.cause = cause
        self.url = url


# meiva:link ops/docs/ops-module.md
class ClusterOpUnreachable(RuntimeError):  # noqa: N818
    pass


class ClusterOpTargetAbsent(ClusterOpUnreachable):
    pass


# meiva:link ops/docs/ops-module.md
class ClusterOpFailed(RuntimeError):  # noqa: N818
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(f"cluster op failed: {result}")
        self.result = result


@dataclass(frozen=True)
class RpcConfig:
    timeout_seconds: float
    max_retries: int


def rpc_config_from_settings() -> RpcConfig:
    return RpcConfig(
        timeout_seconds=settings.gateway.cluster_rpc_timeout_seconds,
        max_retries=settings.gateway.cluster_rpc_max_retries,
    )


class AsyncClientFactory(Protocol):
    def __call__(self, *, timeout: httpx.Timeout, trust_env: bool) -> httpx.AsyncClient: ...


class GatewayAccessFactory(Protocol):
    def __call__(self) -> GatewayAccess: ...


@dataclass(frozen=True)
class RpcClient:
    config: RpcConfig
    access: GatewayAccessFactory
    timers: Timers
    clients: AsyncClientFactory
    uniform: Callable[[float, float], float]

    @classmethod
    def system(cls) -> Self:
        def access() -> GatewayAccess:
            return gateway_access_from_settings()  # ambient: O

        return cls(
            config=rpc_config_from_settings(),
            access=access,
            timers=SYSTEM_TIMERS,
            clients=httpx.AsyncClient,
            uniform=random.uniform,
        )

    async def _dispatch_once(
        self,
        target_machine: str,
        kind: str,
        payload: dict[str, Any],
        *,
        timeout_s: float,
        ops_url: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        url = f"{ops_url.rstrip('/')}/ops"
        started = self.timers.monotonic()
        access = self.access()
        try:
            headers = gateway_auth_headers(access)
            async with self.clients(
                timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
                trust_env=False,
            ) as client:
                resp = await client.post(
                    url,
                    json=OpEnvelope(
                        kind=kind, payload=payload, idempotency_key=idempotency_key
                    ).model_dump(exclude_none=True),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise _RetryableFailure(exc, url=url) from exc

        _log.info(
            "cluster_rpc %s -> machine=%s responded %d in %.1fs",
            kind,
            target_machine,
            resp.status_code,
            self.timers.monotonic() - started,
        )
        if resp.status_code != 200:
            if 500 <= resp.status_code < 600:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise _RetryableFailure(exc, url=url) from exc
            raise ClusterOpUnreachable(
                f"ops server for machine={target_machine!r} returned {resp.status_code} at {url}: "
                f"{resp.text[:200]}"
            )

        try:
            envelope = OpResponse.model_validate(resp.json())
        except ValidationError as exc:
            raise ClusterOpUnreachable(
                f"ops server for machine={target_machine!r} returned a malformed response at {url}: "
                f"{resp.text[:200]}"
            ) from exc
        if envelope.status is OpStatus.COMPLETED:
            return envelope.result
        raise ClusterOpFailed(envelope.result)

    async def dispatch_to_url(
        self,
        target_machine: str,
        kind: str,
        payload: dict[str, Any],
        *,
        ops_url: str,
        timeout_s: float | None = None,
        retries: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not is_op_kind(kind):
            raise ValueError(f"unknown op kind: {kind!r}")
        if timeout_s is None:
            timeout_s = self.config.timeout_seconds

        if kind in NON_IDEMPOTENT_KINDS and idempotency_key is None:
            idempotency_key = default_idempotency_key(target_machine, kind, payload)
        if retries is None:
            retries = self.config.max_retries
        retries = max(retries, 0)

        started = self.timers.monotonic()
        last: _RetryableFailure | None = None
        for attempt in range(retries + 1):
            try:
                return await self._dispatch_once(
                    target_machine,
                    kind,
                    payload,
                    timeout_s=timeout_s,
                    ops_url=ops_url,
                    idempotency_key=idempotency_key,
                )
            except _RetryableFailure as exc:
                last = exc
                if attempt < retries:
                    delay = retry_delay_s(attempt, uniform=self.uniform)
                    _log.debug(
                        "cluster_rpc %s -> machine=%s attempt %d/%d failed, retrying in %.1fs: %r",
                        kind,
                        target_machine,
                        attempt + 1,
                        retries + 1,
                        delay,
                        exc.cause,
                    )
                    await self.timers.asleep(delay)
        if last is None:
            raise RuntimeError(
                f"cluster_rpc retry loop exhausted without recording a failure "
                f"(machine={target_machine!r}, kind={kind!r})"
            )
        log = _log.debug if kind == "status_probe" else _log.warning
        log(
            "cluster_rpc %s -> machine=%s unreachable after %d attempt(s) (%.1fs): %r",
            kind,
            target_machine,
            retries + 1,
            self.timers.monotonic() - started,
            last.cause,
        )
        raise ClusterOpUnreachable(
            f"ops server for machine={target_machine!r} unreachable at {last.url} "
            f"after {retries + 1} attempt(s): {last.cause!r}"
        ) from last.cause


async def dispatch_to_machine(
    db: Database,
    target_machine: str,
    kind: str,
    payload: dict[str, Any],
    *,
    timeout_s: float | None = None,
    retries: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not is_op_kind(kind):
        raise ValueError(f"unknown op kind: {kind!r}")
    try:
        ops_url = lookup_machine_url(db, target_machine)
    except MachineNotRegistered as exc:
        raise ClusterOpTargetAbsent(
            f"machine={target_machine!r} is absent from the machines registry "
            f"(cannot resolve an address): {exc}"
        ) from exc
    except MachineGatewayUrlMissing as exc:
        raise ClusterOpUnreachable(
            f"cannot resolve an address for machine={target_machine!r}: {exc}"
        ) from exc
    return await dispatch_to_url(
        target_machine,
        kind,
        payload,
        ops_url=ops_url,
        timeout_s=timeout_s,
        retries=retries,
        idempotency_key=idempotency_key,
    )


async def dispatch_to_url(
    target_machine: str,
    kind: str,
    payload: dict[str, Any],
    *,
    ops_url: str,
    timeout_s: float | None = None,
    retries: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not is_op_kind(kind):
        raise ValueError(f"unknown op kind: {kind!r}")
    return await RpcClient.system().dispatch_to_url(
        target_machine,
        kind,
        payload,
        ops_url=ops_url,
        timeout_s=timeout_s,
        retries=retries,
        idempotency_key=idempotency_key,
    )

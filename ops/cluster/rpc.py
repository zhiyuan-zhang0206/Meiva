# meiva:note
# The cluster RPC client: one gateway-to-runner (or runner-to-runner) control op over HTTP.
#
# `RpcClient` is the owner of one dispatch's transport — the credential read, the timers for the
# retry loop and the jitter draw. What a dispatch tries, waits and sends is decided from values in
# `ops.cluster.rpc_plans`. `RpcClient.system()` is the composition entry that reads the process
# settings; the module-level `dispatch_to_machine` / `dispatch_to_url` are its one-shot shorthand
# for callers that hold no client of their own.

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self, assert_never

import httpx

from base.clock import SYSTEM_TIMERS, Timers
from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.cluster.machine.ambient import gateway_access_from_settings
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machines import (
    MachineGatewayUrlMissing,
    MachineNotRegistered,
)
from base.cluster.machines import (
    lookup as lookup_machine_url,
)
from base.config import ConfigBoot, settings
from base.db import Database
from ops.cluster.rpc_plans import (
    JITTER_HIGH,
    JITTER_LOW,
    ResponseVerdict,
    RpcConfig,
    business_idempotency_key,
    connect_timeout_s,
    dispatch_budget,
    response_verdict,
    retry_delay_s,
    worst_case_dispatch_seconds,
)
from ops.cluster.rpc_plans import (
    NON_IDEMPOTENT_KINDS as NON_IDEMPOTENT_KINDS,
)
from ops.rpc_schemas import OpEnvelope, OpKind, OpResponse, OpStatus, is_op_kind

__all__ = [
    "ClusterOpFailed",
    "ClusterOpTargetAbsent",
    "ClusterOpUnreachable",
    "RpcClient",
    "RpcConfig",
    "default_idempotency_key",
    "dispatch_to_machine",
    "dispatch_to_url",
    "rpc_config_from_settings",
    "worst_case_dispatch_seconds",
]

_log = logging.getLogger(__name__)


# meiva:note
# The key a dispatch of `kind` carries when the caller supplied none: the stable business key
# where the payload has a business id, else a fresh one per dispatch.
def default_idempotency_key(target_machine: str, kind: OpKind, payload: dict[str, Any]) -> str:
    return business_idempotency_key(target_machine, kind, payload) or f"{kind}:{uuid.uuid4()}"


# meiva:note
# The names of the dispatch's failures are its public vocabulary (`ClusterOpFailed`,
# `ClusterOpUnreachable`, `ClusterOpTargetAbsent`) and read as conditions, not as `...Error`
# types, which is why the exception-suffix naming rule is waived on them.
# meiva:link ops/docs/ops-module.md
class _RetryableFailure(Exception):  # noqa: N818
    def __init__(self, cause: Exception, *, url: str) -> None:
        super().__init__(f"{url}: {cause!r}")
        self.cause = cause
        self.url = url


# meiva:note
# The op could not be delivered: the ops server was unreachable, answered with a terminal HTTP
# status, or returned a response that is not an op envelope. The class name reads as a condition,
# so the exception-suffix naming rule is waived.
# meiva:link ops/docs/ops-module.md
class ClusterOpUnreachable(RuntimeError):  # noqa: N818
    pass


class ClusterOpTargetAbsent(ClusterOpUnreachable):
    pass


# meiva:note
# The ops server ran the op and reported it failed; `result` is the failure payload for the
# consumer to translate. The class name reads as a condition, so the exception-suffix naming rule
# is waived.
# meiva:link ops/docs/ops-module.md
class ClusterOpFailed(RuntimeError):  # noqa: N818
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(f"cluster op failed: {result}")
        self.result = result


def rpc_config_from_settings() -> RpcConfig:
    return RpcConfig(
        timeout_seconds=settings.gateway.cluster_rpc_timeout_seconds,
        max_retries=settings.gateway.cluster_rpc_max_retries,
    )


def _access_from_settings() -> GatewayAccess:
    return gateway_access_from_settings()  # ambient: O


# meiva:note
# One dispatch's transport owner.
#
# `access` is a required, concretely typed lazy read, called on every attempt including retries:
# the gateway credential is re-read from the process configuration each time, so a credential that
# was refreshed between attempts is the one presented (authentication keeps its existing refresh
# lifetime). `timers` is the retry loop's clock: the loop interleaves attempts with waits, so it
# owns its sleeps. The HTTP client is the real `httpx.AsyncClient`, built per attempt with the
# system proxy settings ignored (`trust_env=False`) because cluster peers are private-network
# addresses.
@dataclass(frozen=True)
class RpcClient:
    config: RpcConfig
    access: Callable[[], GatewayAccess]
    timers: Timers

    @classmethod
    def system(cls) -> Self:
        return cls(
            config=rpc_config_from_settings(), access=_access_from_settings, timers=SYSTEM_TIMERS
        )

    # meiva:note
    # The client a root builds from its own configuration: the dispatch budget from the
    # `gateway` fields, and the gateway access read from `config` on every attempt (a credential
    # refreshed between attempts is the one presented). The root hands in `timers`; nothing here
    # reads a process singleton.
    @classmethod
    def from_boot(cls, config: ConfigBoot, *, timers: Timers) -> Self:
        gateway = config.view.gateway
        return cls(
            config=RpcConfig(
                timeout_seconds=gateway.cluster_rpc_timeout_seconds,
                max_retries=gateway.cluster_rpc_max_retries,
            ),
            access=lambda: gateway_access_from_boot(config),
            timers=timers,
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
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s(timeout_s)),
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
        verdict = response_verdict(resp.status_code)
        match verdict:
            case ResponseVerdict.ANSWERED:
                pass
            case ResponseVerdict.TRANSIENT:
                raise _RetryableFailure(
                    httpx.HTTPStatusError(
                        f"server error {resp.status_code} for url {url!r}",
                        request=resp.request,
                        response=resp,
                    ),
                    url=url,
                )
            case ResponseVerdict.REFUSED:
                raise ClusterOpUnreachable(
                    f"ops server for machine={target_machine!r} returned {resp.status_code} "
                    f"at {url}: {resp.text[:200]}"
                )
            case _:
                assert_never(verdict)

        try:
            envelope = OpResponse.model_validate(resp.json())
        except ValueError as exc:
            # meiva:note
            # A 200 whose body is not an op envelope is a malformed answer: a body that is not
            # JSON (a proxy's HTML page, a truncated write, bytes that are not UTF-8) raises
            # a `ValueError` subclass from `resp.json()`, and one that is JSON of the wrong
            # shape raises pydantic's `ValidationError`, also a `ValueError`. Both read as
            # the same unreachable-ops-server condition instead of leaking a decoder error.
            raise ClusterOpUnreachable(
                f"ops server for machine={target_machine!r} returned a malformed response at {url}: "
                f"{resp.text[:200]}"
            ) from exc
        if envelope.status is OpStatus.COMPLETED:
            return envelope.result
        raise ClusterOpFailed(envelope.result)

    # meiva:note
    # Dispatch one op to an already resolved ops address, retrying transient failures.
    #
    # Transient HTTP transport errors and 5xx responses retry with the bounded jittered backoff
    # of `retry_delay_s`; business failures (`ClusterOpFailed`), 4xx responses and invalid
    # response envelopes are terminal. The internal retry signal never escapes: exhaustion raises
    # `ClusterOpUnreachable` with the last transport cause. Probe exhaustion logs at DEBUG; other
    # operation failures remain WARNING. An operation in `NON_IDEMPOTENT_KINDS` carries one
    # idempotency key across every attempt.
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
        budget = dispatch_budget(self.config, timeout_s, retries)

        if kind in NON_IDEMPOTENT_KINDS and idempotency_key is None:
            idempotency_key = default_idempotency_key(target_machine, kind, payload)

        started = self.timers.monotonic()
        last: _RetryableFailure | None = None
        for attempt in range(budget.retries + 1):
            try:
                return await self._dispatch_once(
                    target_machine,
                    kind,
                    payload,
                    timeout_s=budget.timeout_s,
                    ops_url=ops_url,
                    idempotency_key=idempotency_key,
                )
            except _RetryableFailure as exc:
                last = exc
                if attempt < budget.retries:
                    # meiva:note
                    # The jitter is a scheduling spread, not a secret, so the non-cryptographic
                    # `random` draw is the right one (S311 is about security tokens).
                    delay = retry_delay_s(attempt, random.uniform(JITTER_LOW, JITTER_HIGH))  # noqa: S311
                    _log.debug(
                        "cluster_rpc %s -> machine=%s attempt %d/%d failed, retrying in %.1fs: %r",
                        kind,
                        target_machine,
                        attempt + 1,
                        budget.retries + 1,
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
            budget.retries + 1,
            self.timers.monotonic() - started,
            last.cause,
        )
        raise ClusterOpUnreachable(
            f"ops server for machine={target_machine!r} unreachable at {last.url} "
            f"after {budget.retries + 1} attempt(s): {last.cause!r}"
        ) from last.cause

    # meiva:note
    # Dispatch one op to a registered machine: resolve its ops address from the `machines`
    # registry, then `dispatch_to_url`.
    #
    # A missing registered machine raises `ClusterOpTargetAbsent`, a definitive absence that
    # terminal-state consumers can handle separately; it remains a `ClusterOpUnreachable`
    # subtype for existing transport handlers. A registered machine without an ops URL is
    # unreachable; neither condition starts HTTP or reads credentials. Unknown operation kinds
    # fail before lookup, credential access or idempotency-key construction.
    async def dispatch_to_machine(
        self,
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
        return await self.dispatch_to_url(
            target_machine,
            kind,
            payload,
            ops_url=ops_url,
            timeout_s=timeout_s,
            retries=retries,
            idempotency_key=idempotency_key,
        )


# meiva:note
# `RpcClient.system().dispatch_to_machine(...)` for a caller that holds no client of its own.
# The kind is checked first; a valid call reads settings once through `RpcClient.system()`.
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
    return await RpcClient.system().dispatch_to_machine(
        db,
        target_machine,
        kind,
        payload,
        timeout_s=timeout_s,
        retries=retries,
        idempotency_key=idempotency_key,
    )


# meiva:note
# `RpcClient.system().dispatch_to_url(...)` for a caller that holds no client of its own. The
# kind is checked first; a valid call reads settings once through `RpcClient.system()`.
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

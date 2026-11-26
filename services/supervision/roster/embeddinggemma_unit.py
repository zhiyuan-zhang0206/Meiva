# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from base.cluster.listener_plan import TcpBind
from base.daemon.health import DaemonProbe
from services.derived.embeddinggemma.config import MODULE, launch_command
from services.derived.embeddinggemma.contract import (
    DEFAULT_MODEL,
    HEALTH_PATH,
    LOOPBACK,
    SHUTDOWN_CEILING_S,
)
from services.derived.embeddinggemma.launch import (
    EmbeddingGemmaSettings,
    launch_problem,
    server_config,
)
from services.supervision.roster.service_spec import BOTH, ServiceSpec

SESSION = "embeddinggemma"
DEFAULT_PYTHON = ".venv/bin/python"
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "[::]"})  # noqa: S104


def probe_host(bind_host: str) -> str:
    return LOOPBACK if bind_host in _WILDCARD_BINDS else bind_host


def embeddinggemma_service(
    settings: EmbeddingGemmaSettings,
    *,
    self_host: str,
    probe: Callable[..., DaemonProbe],
) -> ServiceSpec:
    launchable = launch_problem(settings, self_host=self_host) is None
    python = settings.python or DEFAULT_PYTHON
    port_valid = 0 < settings.port < 65536
    if launchable:
        server = server_config(settings)
        cmd = launch_command(server, python=python)
        bind = server.bind_host
        model = server.model
    else:
        cmd = f"{python} -m {MODULE}"
        bind = settings.bind_host or LOOPBACK
        model = DEFAULT_MODEL if settings.model is None else settings.model
    url = f"http://{probe_host(bind)}:{settings.port}{HEALTH_PATH}"
    return ServiceSpec(
        session=SESSION,
        cmd=cmd,
        capabilities=BOTH,
        requires_db=False,
        tcp_port=settings.port if port_valid else None,
        listen_binds=(lambda: (TcpBind(bind, settings.port),)) if port_valid else None,
        identity_probe=partial(
            probe, url=url, model=model, revision=settings.revision, device=settings.device
        ),
        healthcheck_module="services.derived.embeddinggemma.probe",
        stop_ceiling_s=SHUTDOWN_CEILING_S,
    )

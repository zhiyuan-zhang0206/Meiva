# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from base.cluster.listener_plan import TcpBind
from base.daemon.health import DaemonProbe
from services.derived.embeddinggemma.config import MODULE, ServerConfig, launch_command
from services.derived.embeddinggemma.contract import (
    DEFAULT_MODEL,
    HEALTH_PATH,
    LOOPBACK,
    parse_device,
)
from services.derived.embeddinggemma.placement import hosting_problem
from services.supervision.roster.field_values import optional_text, text, whole
from services.supervision.roster.service_spec import BOTH, ServiceSpec

SESSION = "embeddinggemma"
DEFAULT_PYTHON = ".venv/bin/python"
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "[::]"})  # noqa: S104


@dataclass(frozen=True)
class EmbeddingGemmaSettings:
    host: str
    port: int
    device: str
    revision: str | None
    model: str | None
    model_path: str
    cache_dir: str
    python: str
    litert_path: str
    bind_host: str
    max_input_length: int
    api_key_configured: bool

    @classmethod
    def from_fields(cls, read_field: Callable[[str], object]) -> EmbeddingGemmaSettings:

        def field(name: str) -> object:
            return read_field(f"embeddinggemma_{name}")

        return cls(
            host=text(field("host"), "embeddinggemma_host"),
            port=whole(field("port"), "embeddinggemma_port"),
            device=text(field("device"), "embeddinggemma_device"),
            revision=optional_text(field("revision"), "embeddinggemma_revision"),
            model=optional_text(field("model"), "embeddinggemma_model"),
            model_path=str(field("model_path")),
            cache_dir=str(field("cache_dir")),
            python=text(field("python"), "embeddinggemma_python").strip(),
            litert_path=text(field("litert_path"), "embeddinggemma_litert_path").strip(),
            bind_host=text(field("bind_host"), "embeddinggemma_bind_host").strip(),
            max_input_length=whole(field("max_input_length"), "embeddinggemma_max_input_length"),
            api_key_configured=read_field("embeddinggemma_api_key") is not None,
        )

    def server(self) -> ServerConfig:
        return ServerConfig(
            model_path=self.model_path,
            device=parse_device(self.device),
            revision=self.revision or "",
            model=DEFAULT_MODEL if self.model is None else self.model,
            cache_dir=self.cache_dir,
            max_input_length=self.max_input_length,
            bind_host=self.bind_host,
            port=self.port,
            litert_path=self.litert_path or None,
            require_api_key=self.api_key_configured,
        )


def probe_host(bind_host: str) -> str:
    return LOOPBACK if bind_host in _WILDCARD_BINDS else bind_host


def embeddinggemma_service(
    settings: EmbeddingGemmaSettings,
    *,
    self_host: str,
    probe: Callable[..., DaemonProbe],
) -> ServiceSpec:
    hosted = hosting_problem(settings.host, self_host=self_host, revision=settings.revision) is None
    python = settings.python or DEFAULT_PYTHON
    if hosted:
        server = settings.server()
        cmd = launch_command(server, python=python)
        bind = server.bind_host
        model = server.model
    else:
        cmd = f"{python} -m {MODULE}"
        bind = settings.bind_host
        model = DEFAULT_MODEL if settings.model is None else settings.model
    url = f"http://{probe_host(bind)}:{settings.port}{HEALTH_PATH}"
    return ServiceSpec(
        session=SESSION,
        cmd=cmd,
        capabilities=BOTH,
        requires_db=False,
        tcp_port=settings.port,
        listen_binds=lambda: (TcpBind(bind, settings.port),),
        identity_probe=partial(
            probe, url=url, model=model, revision=settings.revision, device=settings.device
        ),
        healthcheck_module="services.derived.embeddinggemma.probe",
    )

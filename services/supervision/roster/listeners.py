from pathlib import Path

from base.cluster.listener_plan import TcpBind
from base.cluster.machine import MachineRoles
from base.host.db_authority.unit import telemetry_bearer
from base.telemetry.otlp.collector_listeners import (
    LOCAL_INGRESS_HOST,
    SELF_METRICS_HOST,
    remote_ingress_host,
)


def collector_binds(
    *, home: Path, roles: MachineRoles | None, host: str, ingress_port: int, metrics_port: int
) -> tuple[TcpBind, ...]:
    local = (TcpBind(LOCAL_INGRESS_HOST, ingress_port), TcpBind(SELF_METRICS_HOST, metrics_port))
    remote = remote_ingress_host(roles, host=host, has_token=bool(telemetry_bearer(home.resolve())))
    return local if remote is None else (*local, TcpBind(remote, ingress_port))

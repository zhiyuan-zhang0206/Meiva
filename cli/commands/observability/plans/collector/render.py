# meiva:note
# The collector config template with every value filled in.

from __future__ import annotations

from dataclasses import dataclass
from string import Template

from cli.commands.observability.plans.collector.remote import RemoteFragments

__all__ = ["CollectorValues", "render_collector_config"]


# meiva:note
# Every value the collector template substitutes.
#
# `data_plane_receivers` and `data_plane_pipeline` are the fragments for this unit's own Postgres
# and Redis, `exporters` is the role-specific fan-out block, and `tempo_endpoint`, `loki_base` and
# `prom_base` stay as named generation inputs for small downstream or custom templates: the
# shipped template consumes the `exporters` block.
@dataclass(frozen=True)
class CollectorValues:
    meiva_home: str
    cluster_label: str
    machine_name: str
    tempo_endpoint: str
    loki_base: str
    prom_base: str
    retention_days: int
    self_metrics_port: int
    local_ingress_endpoint: str
    data_plane_receivers: str
    data_plane_pipeline: str
    exporters: str
    remote: RemoteFragments


# meiva:note
# The rendered collector config. A placeholder the template names and the values do not carry
# raises `KeyError`, so a template that outgrew the generator fails loudly.
def render_collector_config(template: str, values: CollectorValues) -> str:
    substitutions = {
        "MEIVA_HOME": values.meiva_home,
        "CLUSTER_LABEL": values.cluster_label,
        "MACHINE_NAME": values.machine_name,
        "TEMPO_ENDPOINT": values.tempo_endpoint,
        "LOKI_BASE": values.loki_base,
        "PROM_BASE": values.prom_base,
        "RETENTION_DAYS": str(values.retention_days),
        "SELF_METRICS_PORT": str(values.self_metrics_port),
        "OTLP_RECEIVER_ENDPOINT": values.local_ingress_endpoint,
        "DATA_PLANE_RECEIVERS": values.data_plane_receivers,
        "DATA_PLANE_PIPELINE_RECEIVERS": values.data_plane_pipeline,
        "OTLP_EXPORTERS": values.exporters,
        "REMOTE_OTLP_RECEIVER": values.remote.receiver,
        "CLUSTER_AUTH_EXTENSION": values.remote.auth_extension,
        "CLUSTER_AUTH_SERVICE_EXTENSION": values.remote.auth_service_extension,
        "REMOTE_OTLP_PIPELINE_RECEIVER": values.remote.pipeline_receiver,
        "REMOTE_TRACE_PIPELINE": values.remote.trace_pipeline,
    }
    return Template(template).substitute(substitutions)

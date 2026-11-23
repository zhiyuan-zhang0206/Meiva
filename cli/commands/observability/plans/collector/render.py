# meiva:note
# The collector config template with every value filled in.
# Fallbacks edit YAML nodes so the operator's scalar encodings survive re-emission.

from __future__ import annotations

from dataclasses import dataclass
from string import Template

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of
from cli.commands.observability.plans.collector.remote import RemoteFragments

__all__ = ["CollectorValues", "render_collector_config", "without_harness_receiver"]


_HARNESS_RECEIVER = """  otlp/harness:
    protocols:
      http:
        endpoint: {endpoint}
        traces_url_path: /meiva-harness/v1/traces
        logs_url_path: /meiva-harness/v1/logs
        metrics_url_path: /meiva-harness/v1/metrics
        response_headers:
          X-Meiva-Harness-Redaction: "1"
""".rstrip()

_HARNESS_PIPELINES = """    traces/harness:
      receivers: [otlp/harness]
      processors: [redaction/harness, transform/harness_payload]
      exporters: [forward/harness]
    logs/harness:
      receivers: [otlp/harness]
      processors: [transform/harness_payload, redaction/harness]
      exporters: [forward/harness]
    metrics/harness:
      receivers: [otlp/harness]
      processors: [redaction/harness, transform/harness_payload]
      exporters: [forward/harness]"""


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
    harness_ingress_endpoint: str | None


# meiva:note
# The rendered collector config. A placeholder the template names and the values do not carry
# raises `KeyError`, so a template that outgrew the generator fails loudly.
def render_collector_config(template: str, values: CollectorValues) -> str:
    harness_endpoint = values.harness_ingress_endpoint
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
        "HARNESS_OTLP_RECEIVER": (
            _HARNESS_RECEIVER.format(endpoint=harness_endpoint) if harness_endpoint else ""
        ),
        "HARNESS_OTLP_CONNECTOR": "connectors:\n  forward/harness: {}" if harness_endpoint else "",
        "HARNESS_OTLP_PIPELINES": _HARNESS_PIPELINES if harness_endpoint else "",
        "HARNESS_OTLP_FORWARD": ", forward/harness" if harness_endpoint else "",
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


def _node_value(node: Node) -> object:
    return object_of(getattr, node, "value")


def _entries(loader: yaml.SafeLoader, node: MappingNode) -> dict[str, tuple[ScalarNode, Node]]:
    loader.flatten_mapping(node)
    pairs = _node_value(node)
    if not is_object_list(pairs):
        raise ValueError("expected YAML mapping entries")
    entries: dict[str, tuple[ScalarNode, Node]] = {}
    for pair in pairs:
        match pair:
            case (ScalarNode() as key, Node() as value):
                entries[loader.construct_scalar(key)] = (key, value)
            case _:
                raise ValueError("invalid YAML mapping entry")
    node.value = list(entries.values())
    return entries


def _mapping_at(loader: yaml.SafeLoader, node: Node | None, name: str) -> MappingNode | None:
    if not isinstance(node, MappingNode):
        return None
    entry = _entries(loader, node).get(name)
    return entry[1] if entry is not None and isinstance(entry[1], MappingNode) else None


def _drop_harness_pipelines(loader: yaml.SafeLoader, pipelines: MappingNode) -> None:
    entries = _entries(loader, pipelines)
    for signal in ("traces", "logs", "metrics"):
        _ = entries.pop(f"{signal}/harness", None)
    pipelines.value = list(entries.values())
    for _, pipeline in entries.values():
        if not isinstance(pipeline, MappingNode):
            continue
        for name, (_, components) in _entries(loader, pipeline).items():
            if name in ("receivers", "exporters") and isinstance(components, SequenceNode):
                _drop_harness_components(components)


def _drop_harness_components(components: SequenceNode) -> None:
    values = _node_value(components)
    if not is_object_list(values):
        raise ValueError("expected YAML pipeline components")
    components.value = [
        node
        for node in values
        if isinstance(node, Node) and _node_value(node) not in ("forward/harness", "otlp/harness")
    ]


def _serialize(root: Node) -> str:
    serialize = object_of(getattr, yaml, "serialize")
    if not callable(serialize):
        raise TypeError("PyYAML serializer unavailable")
    rendered = object_of(serialize, root, Dumper=yaml.SafeDumper)
    if not isinstance(rendered, str):
        raise TypeError("expected YAML text")
    return rendered


def _without_harness(loader: yaml.SafeLoader) -> str | None:
    root = loader.get_single_node()
    if root is None:
        return None
    receivers = _mapping_at(loader, root, "receivers")
    if receivers is None:
        return None
    entries = _entries(loader, receivers)
    if entries.pop("otlp/harness", None) is None:
        return None
    receivers.value = list(entries.values())
    connectors = _mapping_at(loader, root, "connectors")
    if connectors is not None:
        entries = _entries(loader, connectors)
        _ = entries.pop("forward/harness", None)
        connectors.value = list(entries.values())
    service = _mapping_at(loader, root, "service")
    pipelines = _mapping_at(loader, service, "pipelines")
    if pipelines is not None:
        _drop_harness_pipelines(loader, pipelines)
    return _serialize(root)


def without_harness_receiver(content: str) -> str | None:
    try:
        return _without_harness(yaml.SafeLoader(content))
    except (yaml.YAMLError, RecursionError):
        raise ValueError("cannot prepare collector fallback: config.yaml is invalid YAML") from None

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


def _entries(node: MappingNode) -> list[tuple[Node, Node]]:
    pairs = _node_value(node)
    if not is_object_list(pairs):
        raise ValueError("expected YAML mapping entries")
    entries: list[tuple[Node, Node]] = []
    for pair in pairs:
        match pair:
            case (Node() as key, Node() as value):
                entries.append((key, value))
            case _:
                raise ValueError("invalid YAML mapping entry")
    return entries


def _mapping_at(node: Node | None, name: str) -> MappingNode | None:
    if isinstance(node, MappingNode):
        for key, value in _entries(node):
            if (
                isinstance(key, ScalarNode)
                and _node_value(key) == name
                and isinstance(value, MappingNode)
            ):
                return value
    return None


def _drop_harness_pipelines(pipelines: MappingNode) -> None:
    pipelines.value = [
        (key, value)
        for key, value in _entries(pipelines)
        if _node_value(key) not in ("traces/harness", "logs/harness", "metrics/harness")
    ]
    for _, pipeline in _entries(pipelines):
        if not isinstance(pipeline, MappingNode):
            continue
        for key, components in _entries(pipeline):
            if _node_value(key) in ("receivers", "exporters") and isinstance(
                components, SequenceNode
            ):
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


def without_harness_receiver(content: str) -> str | None:
    try:
        root = yaml.SafeLoader(content).get_single_node()
    except yaml.YAMLError:
        raise ValueError("cannot prepare collector fallback: config.yaml is invalid YAML") from None
    if root is None:
        return None
    receivers = _mapping_at(root, "receivers")
    if receivers is None or _mapping_at(receivers, "otlp/harness") is None:
        return None
    receivers.value = [
        (key, value) for key, value in _entries(receivers) if _node_value(key) != "otlp/harness"
    ]
    connectors = _mapping_at(root, "connectors")
    if connectors is not None:
        connectors.value = [
            (key, value)
            for key, value in _entries(connectors)
            if _node_value(key) != "forward/harness"
        ]
    pipelines = _mapping_at(_mapping_at(root, "service"), "pipelines")
    if pipelines is not None:
        _drop_harness_pipelines(pipelines)
    return _serialize(root)

# meiva:note
# Core metrics — the first-party observability surface. Each definition module returns its specs
# from `core_metrics()` and `collect_core_metrics` runs the same template safety validation as
# plugin metrics with `plugin = "core"`.
# meiva:link deploy/lgtm/config/grafana/provisioning/dashboards/README.md

from __future__ import annotations

from collections.abc import Sequence
from types import ModuleType
from typing import Protocol, TypeGuard

from base.telemetry.metrics.core import (
    ci,
    cost,
    dismissed,
    events,
    host,
    main_red,
    observability,
    panels,
)
from base.telemetry.metrics.plugin_metrics import (
    DuplicateMetric,
    MetricSpec,
    validate_spec_sql,
)

# Definition modules collected (in registration order across modules). The
# renderer orders the dashboard by each spec's ``section`` + ``order`` pins,
# not this sequence — it only breaks ties for un-pinned specs and shapes the
# inspector listing. Every module is required: a definition module that cannot
# be imported fails the collection rather than silently emptying its section.
CORE_DEFINITION_MODULES: tuple[ModuleType, ...] = (
    panels,
    main_red,
    cost,
    dismissed,
    *ci.DELIVERY_MODULES,
    ci,
    events,
    host,
    observability,
    *observability.PACK_MODULES,
)


def validate_core_metric(spec: MetricSpec) -> MetricSpec:
    """One core metric (first-party observability surface) as the registry holds it.

    Query safety for every template (``validate_spec_sql`` — the same checks
    plugin metrics go through); the ``plugin`` field is ``core``.

    Raises:
        InvalidMetricQuery: any template failed validation.
    """
    validate_spec_sql(spec)
    return spec.model_copy(update={"plugin": "core"})


class _CoreMetricsModule(Protocol):
    def core_metrics(self) -> Sequence[MetricSpec]: ...


def _is_core_metrics_module(module: ModuleType) -> TypeGuard[_CoreMetricsModule]:
    return hasattr(module, "core_metrics")


def collect_core_metrics(
    modules: tuple[ModuleType, ...] = CORE_DEFINITION_MODULES,
) -> list[MetricSpec]:
    """The core metrics of every definition module, validated, in registration order.

    Raises:
        DuplicateMetric: two definitions share a name (names are global across core metrics).
        InvalidMetricQuery: any template failed validation.
    """
    collected: dict[str, MetricSpec] = {}
    for module in modules:
        if not _is_core_metrics_module(module):
            raise TypeError(f"core metric module {module!r} has no core_metrics()")
        for spec in module.core_metrics():
            if spec.name in collected:
                raise DuplicateMetric(
                    f"core metric {spec.name!r} already registered — names are global across core metrics."
                )
            collected[spec.name] = validate_core_metric(spec)
    return list(collected.values())

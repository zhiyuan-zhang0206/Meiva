# meiva:note
# Core Fleet growth panels: reads the gateway's 60s `agent_registry` high-water mark gauge
# (`meiva_agent_registry_max_id_ratio`) and its daily growth slope (`deriv() * 86400`).

from __future__ import annotations

from base.telemetry.metrics.plugin_metrics import MetricSpec


def core_metrics() -> list[MetricSpec]:
    """This module's core metrics, in registration order."""
    specs: list[MetricSpec] = []
    specs.append(
        MetricSpec(
            name="core_agent_max_id",
            title="Max Agent ID",
            description=(
                "Agents-table high-water mark (max id), sampled by the gateway every "
                "60s into the meiva_agent_registry_max_id_ratio gauge (task #2010); a "
                "vertical step marks a batch spawn."
            ),
            event_name="agent_registry",
            category="telemetry",
            unit="short",
            panel="timeseries",
            # meiva:note
            # Unit-"1" gauges export with the `_ratio` suffix. The gauge is per gateway process,
            # so `max()` collapses overlapping rollout series into one high-water curve.
            query="max(meiva_agent_registry_max_id_ratio)",
            query_type="promql",
            target_names=["max_id"],
            panel_id=2301,
            section="Fleet",
            order=5,
        )
    )

    specs.append(
        MetricSpec(
            name="core_agent_max_id_growth_rate",
            title="Max Agent ID growth rate",
            description=(
                "Slope of the meiva_agent_registry_max_id_ratio gauge over the last hour, "
                "expressed in agents per day (deriv() * 86400, task #2010); a spike marks "
                "a batch spawn."
            ),
            event_name="agent_registry",
            category="telemetry",
            unit="short",
            panel="timeseries",
            # The growth curve's slope, extrapolated to agents per day — the
            # deriv() over the last hour on the max()-collapsed 60s gauge (task
            # #2010; the [1h:] subquery form — a bare [1h] range on a function
            # call is a PromQL parse error). Shows batch-spawn intensity at a
            # glance (e.g. +300/day).
            query="deriv(max(meiva_agent_registry_max_id_ratio)[1h:]) * 86400",
            query_type="promql",
            target_names=["agents/day"],
            panel_id=2302,
            section="Fleet",
            order=6,
            field_defaults={"color": {"mode": "fixed", "fixedColor": "purple"}},
        )
    )
    return specs

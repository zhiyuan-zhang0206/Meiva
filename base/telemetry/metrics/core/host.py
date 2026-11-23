"""Core host & data-plane panels — the "Host & data plane" section.

The per-machine gauges the OTel Collector sidecars scrape
(``job="meiva-infra"``: CPU / memory / load / filesystem / disk / network plus
the Postgres and Redis exporters) and the two memory-search export stats
(``meiva_memory_search_stats_*``) — task #3697 S2.

All PromQL against fixed scrape targets; the multi-series panels (per
machine, per mountpoint, multi-direction) carry the series palette
(``palette-classic``), the memory-search pair the fixed blue/purple of their
hand-written originals, and the per-type gauges keep their as-is sizes
(12x8), units, min/max bounds and threshold steps.

"""

from __future__ import annotations

from base.telemetry.metrics.plugin_metrics import MetricSpec, ThresholdStep


def _by_machine(aggregate: str, selection: str, *dimensions: str) -> str:
    grouping = ", ".join(("machine", *dimensions))
    legacy = selection.replace('machine!=""', 'machine="", machine_name!=""')
    return (
        f"{aggregate} by ({grouping}) ({selection} or on ({grouping}) "
        f'label_join({legacy}, "machine", "", "machine_name"))'
    )


def core_metrics() -> list[MetricSpec]:
    """This module's core metrics, in registration order."""
    specs: list[MetricSpec] = []
    specs.append(
        MetricSpec(
            name="core_host_cpu_utilization",
            title="CPU utilization",
            description=(
                "Non-idle CPU averaged over the machine's cores. Alert R8 fires above 0.90 "
                "sustained 15m."
            ),
            event_name="host_metrics",
            category="telemetry",
            unit="percentunit",
            panel="timeseries",
            query_type="promql",
            query="1 - "
            + _by_machine("avg", 'system_cpu_utilization_ratio{machine!="", state="idle"}'),
            target_names=["{{machine}}"],
            field_defaults={"color": {"mode": "palette-classic"}, "max": 1, "min": 0},
            height=8,
            panel_id=2101,
            section="Host & data plane",
            order=0,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_memory_used",
            title="Memory used",
            description=(
                "Used fraction of RAM, reclaimable cache excluded — real pressure. Alert R9 above "
                "0.90 sustained 15m."
            ),
            event_name="host_metrics",
            category="telemetry",
            unit="percentunit",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("avg", 'system_memory_utilization_ratio{machine!="", state="used"}'),
            target_names=["{{machine}}"],
            field_defaults={"color": {"mode": "palette-classic"}, "max": 1, "min": 0},
            height=8,
            panel_id=2102,
            section="Host & data plane",
            order=1,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_load_average",
            title="Load average",
            description=(
                "Runnable-thread load. Compare against the machine's core count, which the CPU "
                "panel normalizes away."
            ),
            event_name="host_metrics",
            category="telemetry",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("max", 'system_cpu_load_average_1m{machine!=""}'),
            targets=[
                _by_machine("max", 'system_cpu_load_average_5m{machine!=""}'),
                _by_machine("max", 'system_cpu_load_average_15m{machine!=""}'),
            ],
            target_names=["{{machine}} 1m", "{{machine}} 5m", "{{machine}} 15m"],
            field_defaults={"color": {"mode": "palette-classic"}},
            height=8,
            panel_id=2103,
            section="Host & data plane",
            order=2,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_filesystem_used",
            title="Filesystem used",
            description=(
                "Per-mountpoint fullness — the pg data dir, the traces mirror and the LGTM volumes "
                "live on these. Alert R10 above 0.90. On macOS every volume of one APFS container "
                "reports the container's fullness."
            ),
            event_name="host_metrics",
            category="telemetry",
            unit="percentunit",
            panel="timeseries",
            query_type="promql",
            query=_by_machine(
                "max", 'system_filesystem_utilization_ratio{machine!=""}', "mountpoint"
            ),
            target_names=["{{machine}} {{mountpoint}}"],
            field_defaults={"color": {"mode": "palette-classic"}, "max": 1, "min": 0},
            height=8,
            panel_id=2104,
            section="Host & data plane",
            order=3,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_disk_throughput",
            title="Disk throughput",
            description="Bytes read/written per second across all devices.",
            event_name="host_metrics",
            category="telemetry",
            unit="Bps",
            panel="timeseries",
            query_type="promql",
            query=_by_machine(
                "sum", 'rate(system_disk_io_bytes_total{machine!=""}[5m])', "direction"
            ),
            target_names=["{{machine}} {{direction}}"],
            field_defaults={"color": {"mode": "palette-classic"}},
            height=8,
            panel_id=2105,
            section="Host & data plane",
            order=4,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_network_throughput",
            title="Network throughput",
            description=(
                "Bytes per second across physical + tunnel interfaces (the sidecar filters macOS's "
                "virtual ones out)."
            ),
            event_name="host_metrics",
            category="telemetry",
            unit="Bps",
            panel="timeseries",
            query_type="promql",
            query=_by_machine(
                "sum", 'rate(system_network_io_bytes_total{machine!=""}[5m])', "direction"
            ),
            target_names=["{{machine}} {{direction}}"],
            field_defaults={"color": {"mode": "palette-classic"}},
            height=8,
            panel_id=2106,
            section="Host & data plane",
            order=5,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_postgres_connections",
            title="Postgres connections",
            description=(
                "Open backends against the server's own max_connections. Alert R11 fires above 80% "
                "of max. PgBouncer has no OTel receiver — pool saturation surfaces here, at "
                "Postgres."
            ),
            event_name="host_metrics",
            category="telemetry",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("max", 'postgresql_backends{machine!=""}'),
            targets=[
                _by_machine("max", 'postgresql_connection_max{machine!=""}'),
            ],
            target_names=["{{machine}} backends", "{{machine}} max_connections"],
            field_defaults={"color": {"mode": "palette-classic"}, "min": 0},
            height=8,
            panel_id=2107,
            section="Host & data plane",
            order=6,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_postgres_transactions",
            title="Postgres transactions",
            description=(
                "Commit and rollback rate. A rollback rate tracking the commit rate means "
                "transactions are failing, not that traffic is low."
            ),
            event_name="host_metrics",
            category="telemetry",
            unit="ops",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("sum", 'rate(postgresql_commits_total{machine!=""}[5m])'),
            targets=[
                _by_machine("sum", 'rate(postgresql_rollbacks_total{machine!=""}[5m])'),
            ],
            target_names=["{{machine}} commits/s", "{{machine}} rollbacks/s"],
            field_defaults={"color": {"mode": "palette-classic"}},
            height=8,
            panel_id=2108,
            section="Host & data plane",
            order=7,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_database_size",
            title="Database size",
            description=(
                "On-disk size of the cluster's own database — the slow half of the disk-watermark "
                "story."
            ),
            event_name="host_metrics",
            category="telemetry",
            unit="bytes",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("max", 'postgresql_db_size_bytes{machine!=""}'),
            target_names=["{{machine}}"],
            field_defaults={"color": {"mode": "palette-classic"}},
            height=8,
            panel_id=2109,
            section="Host & data plane",
            order=8,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_redis_memory",
            title="Redis memory",
            description=(
                "Resident Redis footprint. Alert R12 fires above 2 GiB sustained; rss above used "
                "means fragmentation."
            ),
            event_name="host_metrics",
            category="telemetry",
            unit="bytes",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("max", 'redis_memory_used_bytes{machine!=""}'),
            targets=[
                _by_machine("max", 'redis_memory_rss_bytes{machine!=""}'),
            ],
            target_names=["{{machine}} used", "{{machine}} rss"],
            field_defaults={"color": {"mode": "palette-classic"}},
            height=8,
            panel_id=2110,
            section="Host & data plane",
            order=9,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_redis_clients_evictions",
            title="Redis clients and evictions",
            description=(
                "Any eviction rate above zero means Redis hit maxmemory and is dropping live keys — "
                "cluster state loss, not a cache miss."
            ),
            event_name="host_metrics",
            category="telemetry",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("max", 'redis_clients_connected{machine!=""}'),
            targets=[
                _by_machine("sum", 'rate(redis_keys_evicted_total{machine!=""}[5m])'),
            ],
            target_names=["{{machine}} clients", "{{machine}} evictions/s"],
            field_defaults={"color": {"mode": "palette-classic"}, "min": 0},
            height=8,
            panel_id=2111,
            section="Host & data plane",
            order=10,
        )
    )

    specs.append(
        MetricSpec(
            name="core_host_redis_throughput",
            title="Redis throughput",
            description="Commands per second, as Redis itself reports it.",
            event_name="host_metrics",
            category="telemetry",
            unit="ops",
            panel="timeseries",
            query_type="promql",
            query=_by_machine("max", 'redis_commands_per_second{machine!=""}'),
            target_names=["{{machine}}"],
            field_defaults={"color": {"mode": "palette-classic"}},
            height=8,
            panel_id=2112,
            section="Host & data plane",
            order=11,
        )
    )

    specs.append(
        MetricSpec(
            name="core_memory_search_rows",
            title="Memory search rows",
            description=(
                "Memory-search store row count (absolute state, 60s sample) into the "
                "meiva_memory_search_stats_rows_ratio gauge (task #2088); thresholds: 30k soft "
                "warning / 50k backend-switch evaluation / 100k hard cap."
            ),
            event_name="memory_search_stats",
            category="telemetry",
            panel="timeseries",
            query_type="promql",
            query="max(meiva_memory_search_stats_rows_ratio)",
            target_names=["rows"],
            thresholds=[
                ThresholdStep(color="yellow", value=30000),
                ThresholdStep(color="orange", value=50000),
                ThresholdStep(color="red", value=100000),
            ],
            field_defaults={"color": {"fixedColor": "blue", "mode": "fixed"}},
            panel_id=2303,
            section="Host & data plane",
            order=12,
        )
    )

    specs.append(
        MetricSpec(
            name="core_memory_search_npz_save_duration",
            title="Memory search npz save duration",
            description=(
                "Duration of the most recent npz save (meiva_memory_search_stats_last_save_seconds, "
                "task #2088); absent until the first save after boot — a climbing value flags a "
                "slow save path."
            ),
            event_name="memory_search_stats",
            category="telemetry",
            unit="s",
            panel="timeseries",
            query_type="promql",
            query="max(meiva_memory_search_stats_last_save_seconds)",
            target_names=["last_save"],
            field_defaults={"color": {"fixedColor": "purple", "mode": "fixed"}},
            panel_id=2304,
            section="Host & data plane",
            order=13,
        )
    )
    return specs

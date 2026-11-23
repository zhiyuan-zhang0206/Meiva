from __future__ import annotations


def test_gateway_runtime_payloads_stay_in_the_event_stream() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("sse") == ("mode", "active_connections", "opened", "closed")
    assert payload_keys("gateway_process") == ("cpu_percent", "rss_bytes", "fd_count")
    assert payload_keys("gateway_event_loop") == ("lag_ms", "slow_ticks")
    assert {"sse", "gateway_process", "gateway_event_loop"}.isdisjoint(METRIC_DISPOSITION)


def test_checkpoint_table_sizes_payload_and_metric_disposition() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("checkpoint_table_sizes") == (
        "blobs_bytes",
        "checkpoints_bytes",
        "writes_bytes",
        "blobs_live",
        "checkpoints_live",
        "writes_live",
    )
    assert METRIC_DISPOSITION["checkpoint_table_sizes"] == {
        "blobs_bytes": "gauge",
        "checkpoints_bytes": "gauge",
        "writes_bytes": "gauge",
    }


def test_pr_flow_payload_stays_in_the_event_stream() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("pr_flow_daily") == (
        "day",
        "merged_count",
        "ready_to_merge_median_seconds",
        "ready_to_merge_p90_seconds",
    )
    assert "pr_flow_daily" not in METRIC_DISPOSITION


def test_agent_registry_payload_and_metric_disposition() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("agent_registry") == ("max_id",)
    assert METRIC_DISPOSITION["agent_registry"]["max_id"] == "gauge"


def test_memory_search_stats_payload_and_metric_disposition() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("memory_search_stats") == ("rows", "last_save_seconds")
    assert METRIC_DISPOSITION["memory_search_stats"]["rows"] == "gauge"
    assert METRIC_DISPOSITION["memory_search_stats"]["last_save_seconds"] == "gauge"


def test_root_health_tick_payload_and_metric_disposition() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("root_health_tick") == ("home_id", "last_tick_timestamp_seconds")
    assert METRIC_DISPOSITION["root_health_tick"]["last_tick_timestamp_seconds"] == "gauge"


def test_state_alert_gauges_payload_and_metric_dispositions() -> None:
    from base.events.contract import payload_keys
    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION

    assert payload_keys("trace_recording") == ("disabled",)
    assert METRIC_DISPOSITION["trace_recording"]["disabled"] == "gauge"
    assert payload_keys("backup_dump_status") == ("last_run_ok",)
    assert METRIC_DISPOSITION["backup_dump_status"]["last_run_ok"] == "gauge"
    assert payload_keys("recovery_drill_status") == ("drill", "last_run_ok")
    assert METRIC_DISPOSITION["recovery_drill_status"]["last_run_ok"] == "gauge"
    assert payload_keys("schedule_manager_state") == (
        "stalled",
        "process_lost",
        "convergence_abandoned",
    )
    for field in ("stalled", "process_lost", "convergence_abandoned"):
        assert METRIC_DISPOSITION["schedule_manager_state"][field] == "gauge", field

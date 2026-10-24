# meiva:note
# What trace recording reads from configuration, as one value.
#
# `TraceConfig` is built once by the process root that starts recording
# (`agent/process_boot.py`) from `settings.observability`; nothing below it reads Settings.


from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["TraceConfig", "TraceSettings"]


# meiva:note
# The observability fields recording reads (`ObservabilitySettings` has them all).
class TraceSettings(Protocol):
    trace_enabled: bool
    trace_strip_content: bool
    telemetry_otlp_endpoint: str
    trace_retention_days: int
    trace_max_dir_mb: int
    trace_disk_watermark: float


# meiva:note
# Whether and how a process records traces.
#
# `enabled` is `MEIVA_TRACE_ENABLED`; `strip_content` keeps LLM content out of spans;
# `endpoint` is the local collector's OTLP/HTTP address; `retention_days`, `max_dir_mb`
# and `disk_watermark` bound the on-disk mirror. `endpoint_explicit` is whether the operator
# named the endpoint (`MEIVA_TELEMETRY_OTLP_ENDPOINT` is set), which opens export on a home
# that is not the observability station.
@dataclass(frozen=True)
class TraceConfig:
    enabled: bool
    strip_content: bool
    endpoint: str
    retention_days: int
    max_dir_mb: int
    disk_watermark: float
    endpoint_explicit: bool

    # meiva:note
    # The recording config of a process's observability settings.
    @classmethod
    def of(cls, observability: TraceSettings, *, endpoint_explicit: bool) -> TraceConfig:
        return cls(
            enabled=observability.trace_enabled,
            strip_content=observability.trace_strip_content,
            endpoint=observability.telemetry_otlp_endpoint,
            retention_days=observability.trace_retention_days,
            max_dir_mb=observability.trace_max_dir_mb,
            disk_watermark=observability.trace_disk_watermark,
            endpoint_explicit=endpoint_explicit,
        )

# meiva:note
# Whether a converge pass installs the collector sidecar and what it says about it.
#
# The shell in `otel_collector.py` reads the platform tag, the binary and the version marker on
# disk; these functions decide between installing, keeping and skipping.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "InstallCollector",
    "KeepCollector",
    "SkipUnsupportedPlatform",
    "plan_collector_install",
    "plan_collector_step",
]


# meiva:note
# No pinned collector exists for this platform: warn and skip, and the agent preflight disables
# OTLP export with a reported warning.
@dataclass(frozen=True)
class SkipUnsupportedPlatform:
    platform_label: str

    # meiva:note
    # The warning for the operator.
    def warning(self) -> str:
        return (
            f"  ! otel-collector: no pinned otelcol-contrib for this platform "
            f"({self.platform_label}) — sidecar skipped"
        )


# meiva:note
# Download, verify and install the pinned collector for `tag`.
@dataclass(frozen=True)
class InstallCollector:
    tag: str


# meiva:note
# The pinned collector is already installed.
@dataclass(frozen=True)
class KeepCollector: ...


# meiva:note
# What the install half of a converge pass does.
#
# A binary that exists with a version marker equal to the pin is kept, so an unchanged pin never
# downloads again. The config is regenerated on every pass either way.
def plan_collector_install(
    *,
    tag: str | None,
    platform_label: str,
    binary_present: bool,
    marker: str | None,
    pinned_version: str,
) -> SkipUnsupportedPlatform | InstallCollector | KeepCollector:
    if tag is None:
        return SkipUnsupportedPlatform(platform_label)
    if binary_present and marker is not None and marker.strip() == pinned_version:
        return KeepCollector()
    return InstallCollector(tag)


# meiva:note
# The warnings that explain why this pass skips the collector, or None to install it.
#
# A gateway home is skipped only when it is neither the observability station nor opted into an
# explicit `MEIVA_TELEMETRY_OTLP_ENDPOINT` override: the same rule as the roster gate and the
# sidecar healthcheck. A residual config from an earlier posture is preserved and reported.
def plan_collector_step(
    *,
    gateway_role: bool,
    collector_allowed: bool,
    station_marker: str,
    residual_config: str | None,
) -> tuple[str, ...] | None:
    if not gateway_role or collector_allowed:
        return None
    warnings = [
        "  ! otel-collector: collector skipped — this gateway home is not "
        f"the observability station ({station_marker} is absent and the observability-station "
        "capability is not declared); telemetry export is unavailable; set "
        "MEIVA_TELEMETRY_OTLP_ENDPOINT to use an explicit collector"
    ]
    if residual_config is not None:
        warnings.append(
            f"  ! otel-collector: stale/residual config remains at {residual_config}; "
            "collector files were preserved"
        )
    return tuple(warnings)

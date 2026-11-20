# meiva:note
# The delivery-health definition modules (PR flow and fleet growth), grouped the way
# `observability.PACK_MODULES` groups its packs so the catalog reaches them through one import.
# This module defines no metric of its own.
# meiva:link deploy/lgtm/config/grafana/provisioning/dashboards/README.md
from __future__ import annotations

from types import ModuleType

from base.telemetry.metrics.core import fleet, pr_flow

DELIVERY_MODULES: tuple[ModuleType, ...] = (fleet, pr_flow)

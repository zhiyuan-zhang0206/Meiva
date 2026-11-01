"""Plugin contribution entry point for meiva_usage."""

from __future__ import annotations

__description__ = "Multi-provider LLM usage and quota metrics collector"

from base.packages.plugins.extensions import PluginContributions


def contribute() -> PluginContributions:
    return PluginContributions()

# Task Maintenance

## What it is

A gateway-side daemon registered by the `meiva_fleet` plugin into the ops service roster — overdue task reminders + escalation. The domain belongs entirely to fleet, so it lives under the plugin namespace (`plugins/meiva_fleet/services.py` declares `ServiceSpec`, `services/supervision/roster/plugin_services.py:plugin_services()` discovers and folds it into the single-source `build_services()`).

## Sub-concepts

- [Task Maintenance](task-maintenance/task-maintenance.md)

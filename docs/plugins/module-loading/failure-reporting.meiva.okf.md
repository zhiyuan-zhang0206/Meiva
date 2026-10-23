---
type: doc
title: Plugin load failure reporting
description: Contained plugin load failures, traceback and telemetry reporting, and cancellation boundaries.
tags: [plugins, telemetry]
---

# Plugin load failure reporting

A plugin whose `plugin.py` raises at import (missing sibling, syntax error,
top-level exception) degrades to a **skip with a loud report**, never a
blocked `import meiva` / host boot / graph build: `safe_load_plugin_module`
drops the half-executed module from `sys.modules`, and
`base/packages/plugins/load_report.py:report_plugin_load_failure` emits a loguru ERROR
carrying the traceback plus one `plugin_load_failed` telemetry event (anomaly
tier). Regression coverage distinguishes a synchronous emission exception from
an actual event writer failing after receiving the plugin/error payload. Both
retain the plugin traceback in the local log. Writer barrier/stop failures
propagate through their pipeline owner; the reporter's separate synchronous
emission-exception regression retains its no-recursion fallback. A failed writer
does not establish successful telemetry persistence.
The remaining enabled plugins keep loading; a config entry whose
plugin directory is gone (`DanglingPlugin`) is reported and treated as
disabled. `KeyboardInterrupt` / `SystemExit` still propagate — cancellation is
not a plugin failure.

The same containment applies at other plugin load sites, each
reporting through one reporter: a plugin's `provider.py`
(`base/packages/plugin_providers.py`), `services.py` (`services/supervision/roster/spec.py`), `setup.py`
(`cli/commands/extensions/_plugin_scaffold.py`), a built-in plugin's `metrics.py`
(`gateway/inspect/_plugin_metrics.py`), the gateway plugin inspector's
`inspector.py` (`gateway/inspect/_plugin_widgets.py`). The config face `default_config.py` is read by two more processes through
`base/packages/plugins/config_face.py` (never importing `plugin.py`): `meiva plugins update`
(`plugin_config_admin.update_all_disk_images`; a failing face is an `error` entry on its result, off the
reporter) and the gateway/ops overlay validation (`config_registration.overlay_config_classes`, the enabled
plugins' declared classes, fail-soft).

Child `meiva.ensure_plugins_loaded` enters the same loader. Module-import containment remains separate;
escaping errors abort boot unchanged and are re-raised on later access, without retry. Faces are marked
loaded only after success. The installer isolates only typed declaration refusals after successful
rollback; see [[docs/plugins/declared-contributions.meiva.okf.md]].

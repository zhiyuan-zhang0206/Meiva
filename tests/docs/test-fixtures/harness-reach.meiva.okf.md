---
type: doc
title: "Harness Dependency Reach"
description: "Why root plugins import no production package that only some tests need, and where such fixtures live instead."
tags:
- evaluation
- quality-assurance
---

# Harness Dependency Reach

The PR test selector reads the repo-root `conftest.py` and every `pytest_plugins` module as
a dependency of every test, and it counts a function-local or `TYPE_CHECKING` import like a
top-level one. A root plugin therefore imports a production package only when every test needs
it. A fixture that needs a package for a few consumers lives in the owner's `tests/` and reaches
them through the consumer's conftest import or its directory's `path_scopes.toml`: the CLI
readiness guard (`cli/tests/guards/service_readiness_guard.py`), the helper's native-command guard
(`services/desktop/permissions_helper/tests/permissions_helper/native_io_guard.py`) and the inbound helpers
(`base/agents/inbox/tests/inbound_helpers.py`). Where a global autouse fixture must still
act on a module only some tests load, it acts on the loaded module (`sys.modules.get`) and never
imports it: `_guard_permissions_helper_native_io` refuses the helper door once a conftest has
loaded it, which the CLI's does at collection, and `_telemetry_event_store_off` and `_clean_db`
quiesce only the telemetry already loaded.

Roster and load order of the root plugins: [[test-fixtures.meiva.okf.md]].

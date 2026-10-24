---
type: doc
title: "Harness Dependency Reach"
description: "Why root plugins import no production package that only some tests need, and where such fixtures live instead."
tags:
- evaluation
- quality-assurance
---

# Harness Dependency Reach

Every pytest process loads the repo-root `conftest.py` and its `pytest_plugins` modules.
The PR selector treats this harness as global instead of charging its production closure
to every test; the pinned `scripts/ci/tests/test_harness_imports.py` contract checks that its
production imports remain valid. A root plugin still imports a production package only when
every test needs it. Imports in consumer-local fixtures remain dependency edges, including
function-local and `TYPE_CHECKING` imports.

A fixture that needs a package for a few consumers lives in the owner's `tests/` and reaches
them through the consumer's conftest import or its directory's `path_scopes.toml`: the CLI
readiness guard (`cli/tests/guards/service_readiness_guard.py`), the helper's native-command guard
(`services/desktop/permissions_helper/tests/permissions_helper/native_io_guard.py`) and the inbound helpers
(`base/agents/inbox/tests/inbound_helpers.py`). Where a global autouse fixture must still
act on a module only some tests load, it acts on the loaded module (`sys.modules.get`) and never
imports it: `_guard_permissions_helper_native_io` refuses the helper door once a conftest has
loaded it, which the CLI's does at collection, and `_telemetry_event_store_off` and `_clean_db`
quiesce only the telemetry already loaded.

Roster and load order of the root plugins: [[test-fixtures.meiva.okf.md]].

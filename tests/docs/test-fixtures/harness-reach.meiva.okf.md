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
them through the consumer's conftest import or its directory's `path_scopes.toml`:

- the CLI readiness guard, `cli/tests/guards/service_readiness_guard.py`
- the helper's native-command guard,
  `services/desktop/permissions_helper/tests/permissions_helper/native_io_guard.py`
- the inbound helpers, `base/agents/inbox/tests/inbound_helpers.py`

A global autouse fixture that must guard a package it may not import names the package by
string. The permissions-helper door is guarded that way. `tests/fixtures/guards.py` puts a
finder on `sys.meta_path` that wraps the door's loader, so `converge` is refused the moment
any test or phase first imports the door; the autouse `_guard_permissions_helper_native_io`
refuses a door that is already loaded. `tests/fixtures/tests/test_helper_door_guard.py` pins
both. `_telemetry_event_store_off` and `_clean_db` quiesce only the telemetry already loaded.

Roster and load order of the root plugins: [[test-fixtures.meiva.okf.md]].

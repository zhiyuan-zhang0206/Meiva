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

A global guard that must protect a package it may not import names the package by
string. The permissions-helper door is guarded that way. `tests/fixtures/guards.py` puts a
finder on `sys.meta_path` that wraps the door's loader. A session wrapper refuses `converge`
from collection onward, including aliases bound by `from ... import converge`; a door loaded
before configuration is wrapped immediately too. Only an explicit `native_permissions_helper`
test's complete setup/call/teardown protocol admits the real callable. A `finally` block
revokes that permission after errors or skips, and session cleanup removes the finder and
restores patched names. `tests/fixtures/tests/test_helper_door_guard.py` checks
the real door's wrapper identity and proves refusal/admission with a pure Python fixture door,
without native effects even when the guard regresses.
`_telemetry_event_store_off` and `_clean_db` quiesce only the telemetry already loaded.

Roster and load order of the root plugins: [[test-fixtures.meiva.okf.md]].

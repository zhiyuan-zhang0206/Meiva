---
type: doc
title: "Leak guard — why it is second in pytest_plugins"
description: "The teardown order the guard depends on, and the three ways tests/ci/test_leak_guard.py locks the guard's position in the root plugin stack."
tags:
- evaluation
- quality-assurance
---

# Leak guard — why it is second in `pytest_plugins`

Roster: [[test-leak-guard.meiva.okf.md]].

## Why it is second in `pytest_plugins`

pytest tears fixtures down in reverse setup order. As the first function-scoped autouse fixture to set up, the guard is the last to tear down, so it compares after `monkeypatch` has undone everything it recorded and before any module-scoped or session-scoped fixture finalizes (what those set up is already in the "before" snapshot, so a module's first and last test are not special). Registered after another autouse plugin that uses `monkeypatch` it would compare too early and blame a clean test. `env_bootstrap` stays first: the guard imports only the standard library and pytest. `tests/ci/test_leak_guard.py` locks the position statically (the plugin list), dynamically (the closure of a real test) and by experiment (the same suite with the guard first and last).

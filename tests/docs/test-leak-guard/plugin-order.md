# Leak guard — why it is second in `pytest_plugins`

Roster: [Test Leak Guard — Root Plugin](test-leak-guard.md).

## Why it is second in `pytest_plugins`

pytest tears fixtures down in reverse setup order. As the first function-scoped autouse fixture to set up, the guard is the last to tear down, so it compares after `monkeypatch` has undone everything it recorded and before any module-scoped or session-scoped fixture finalizes (what those set up is already in the "before" snapshot, so a module's first and last test are not special). Registered after another autouse plugin that uses `monkeypatch` it would compare too early and blame a clean test. `env_bootstrap` stays first: the guard imports only the standard library and pytest. `tests/ci/test_leak_guard.py` locks the position statically (the plugin list), dynamically (the closure of a real test) and by experiment (the same suite with the guard first and last).

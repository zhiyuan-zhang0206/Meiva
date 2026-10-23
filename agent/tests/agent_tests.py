"""Shared isolation fixtures for agent tests.

Registered by `tests/fixtures/path_scopes.py`. The cancel-race fixture lives
in `agent.graph.tests.cancel_fixture`; its consumers bind it locally.
"""

from __future__ import annotations

from agent.graph.claim.tests.cursor_fixture import fresh_snapshot_cursor as fresh_snapshot_cursor
from agent.graph.exec.tests.real_child.runner_exec_env import runner_exec_env as runner_exec_env

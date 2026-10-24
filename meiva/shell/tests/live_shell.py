# meiva:note
# One agent's real shell sessions for a test: the pty-sessions service the `pty_service` fixture runs under the test's home, and the throwaway database with the agent's counter row seeded. Nothing is faked: a session a test creates is a real bash behind the service, and its deadline row lands in the real `agent_shell_ttls` table. A test module's conftest imports `live_shell` together with `pty_service`.
# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

from pathlib import Path

import pytest

from base.db import Database
from base.sessions.backend import PtySessionBackend
from base.sessions.pty.client import PtyClientConfig
from base.sessions.pty.tests.pty_service import PtyServiceProcess, service_socket
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions, session_host

LIVE_AGENT_ID = 41


# meiva:note
# Insert the agent and its `agents_meta` row with `session_index` at `next_index`, so the next created session gets that id (the counter is incremented, then one is subtracted).
def seed_agent(database: Database, agent_id: int, *, next_index: int = 0) -> None:
    with database.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (agent_id,))
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, session_index) "
            "VALUES (%s, 'test', 'running', %s) "
            "ON CONFLICT (id) DO UPDATE SET session_index = EXCLUDED.session_index",
            (agent_id, next_index),
        )
        conn.commit()


@pytest.fixture
def live_shell(pty_service: PtyServiceProcess, database: Database) -> ShellSessions:
    del pty_service
    seed_agent(database, LIVE_AGENT_ID)
    return ShellSessions(
        backend=PtySessionBackend(PtyClientConfig(socket_path=service_socket())),
        database=database,
        agent_id=LIVE_AGENT_ID,
    )


# meiva:note
# The machine facts a test session starts from, with the session's HOME set to `home`. A session's login shell then reads the test home's profile and finds the programs installed there, never the developer's own profile or programs.
def isolated_host(home: Path) -> SessionHost:
    host = session_host()
    return SessionHost(checkout=host.checkout, forwarded={**host.forwarded, "HOME": str(home)})


@pytest.fixture
def live_host(unit_home: Path) -> SessionHost:
    return isolated_host(unit_home)

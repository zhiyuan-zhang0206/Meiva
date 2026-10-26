# meiva:note
# Shared fixtures and helpers for the meiva.skills test files. Skills are real directories under
# the per-test unit home's load dir, and the install registry is the real one in that home: a
# test tracks (or disables, or leaves untracked) a skill by writing its registry row, never by
# replacing the function that reads it. Attribution is observed where it lands, in the
# `audit_events` table of the throwaway database, for an agent identity the test pins.

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from base.packages.extensions import install_registry
from base.paths import meiva_home, skills_dir
from meiva.tests.fixtures.pin_agent import pin_agent, snapshot_clients_over


@pytest.fixture
def fake_skills_dir(unit_home: Path) -> Path:
    d = skills_dir(meiva_home())
    d.mkdir()
    return d


# meiva:note
# Write the install-registry rows that let the scanner load these top-level directories.
def track(*names: str, enabled: bool = True) -> None:
    for name in names:
        install_registry.register(
            install_registry.InstalledPackage(name=name, type="skill", enabled=enabled)
        )


# meiva:note
# Track every top-level directory of the load dir as it stands.
def track_present() -> None:
    load_dir = skills_dir(meiva_home())
    if load_dir.is_dir():
        track(*(p.name for p in load_dir.iterdir() if p.is_dir()))


def write_skill(
    root: Path, dirname: str, frontmatter: str, body: str = "", *, tracked: bool = True
) -> None:
    skill_dir = root / dirname
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    load_dir = skills_dir(meiva_home())
    if tracked and root.is_relative_to(load_dir):
        track((root / dirname).relative_to(load_dir).parts[0])


# meiva:note
# An agent identity pinned for the test, and the `skill_invoked` rows it has written.
class AuditedAgent:
    def __init__(self, agent_id: int, conn: psycopg.Connection) -> None:
        self.id = agent_id
        self._conn = conn
        self._seen = 0

    # meiva:note
    # `(skill, depth)` of every row written since the last call, oldest first.
    def invoked(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT attributes FROM audit_events "
            "WHERE agent_id = %s AND event_name = 'skill_invoked' ORDER BY id",
            (self.id,),
        ).fetchall()
        fresh = rows[self._seen :]
        self._seen = len(rows)
        return [
            (str(attributes["skill"]), str(attributes["invocation_depth"]))
            for (attributes,) in fresh
        ]


@pytest.fixture
def audited_agent(db_conn: psycopg.Connection, database_gate: object) -> AuditedAgent:
    agent_id = 10_000_000 + uuid.uuid4().int % 1_000_000_000
    pin_agent(agent_id)
    return AuditedAgent(agent_id, db_conn)


__all__: list[str] = [
    "AuditedAgent",
    "audited_agent",
    "fake_skills_dir",
    "pin_agent",
    "snapshot_clients_over",
    "track",
    "track_present",
    "write_skill",
]

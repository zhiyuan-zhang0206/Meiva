from __future__ import annotations

import pytest

from meizel.contracts.analysis import render_policy
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics

POLICY = (
    render_policy({}, {})
    + """
[ambient]
db = ["connect()", "pool()", "async_pool()", "Database.from_settings()"]
bus = ["EventBus.from_settings()"]
endpoints = ["ServiceEndpoints.from_settings()"]
"""
)
HANDLE = """
class Database:
    @classmethod
    def from_settings(cls):
        return cls()

    @classmethod
    def from_boot(cls, boot):
        return cls()
"""
FILES = {
    "meizel.toml": '[roles]\napplication = ["app", "base"]\n',
    "meizel.policy.toml": POLICY,
    "base/__init__.py": "",
    "base/db/__init__.py": (
        "from base.db.handle import Database as Database\n"
        "def connect(): ...\ndef pool(): ...\ndef async_pool(): ...\n"
    ),
    "base/db/handle.py": HANDLE,
    "base/db/meizel.toml": '[state]\n"__init__.py" = ["db"]\n"handle.py" = ["db"]\n',
    "base/events/__init__.py": "",
    "base/events/live/__init__.py": "from base.events.live.bus import EventBus as EventBus\n",
    "base/events/live/bus.py": HANDLE.replace("Database", "EventBus"),
    "base/events/live/meizel.toml": '[state]\n"__init__.py" = ["bus"]\n"bus.py" = ["bus"]\n',
    "base/daemon/__init__.py": "",
    "base/daemon/endpoints.py": HANDLE.replace("Database", "ServiceEndpoints"),
    "base/daemon/meizel.toml": '[state]\n"endpoints.py" = ["endpoints"]\n',
    "app/__init__.py": "",
}


@pytest.mark.parametrize(
    ("source", "kind", "target"),
    [
        ("from base.db import connect\nx = connect()\n", "db", "base.db.connect"),
        ("from base.db import pool as _pool\nx = _pool()\n", "db", "base.db.pool"),
        ("import base.db as dbm\nx = dbm.async_pool()\n", "db", "base.db.async_pool"),
        ("from base import db\nx = db.connect()\n", "db", "base.db.connect"),
        (
            "from base.db import Database\nx = Database.from_settings()\n",
            "db",
            "base.db.Database.from_settings",
        ),
        (
            "from base.db.handle import Database as D\nx = D.from_settings()\n",
            "db",
            "base.db.handle.Database.from_settings",
        ),
        (
            "import base.db.handle as h\nx = h.Database.from_settings()\n",
            "db",
            "base.db.handle.Database.from_settings",
        ),
        (
            "from base.events.live.bus import EventBus\nx = EventBus.from_settings()\n",
            "bus",
            "base.events.live.bus.EventBus.from_settings",
        ),
        (
            "from base.events.live import EventBus as Bus\nx = Bus.from_settings()\n",
            "bus",
            "base.events.live.EventBus.from_settings",
        ),
        (
            "from base.events.live import bus\nx = bus.EventBus.from_settings()\n",
            "bus",
            "base.events.live.bus.EventBus.from_settings",
        ),
        (
            "from base.daemon.endpoints import ServiceEndpoints\nx = ServiceEndpoints.from_settings()\n",
            "endpoints",
            "base.daemon.endpoints.ServiceEndpoints.from_settings",
        ),
        (
            "from base.daemon.endpoints import ServiceEndpoints as Table\nx = Table.from_settings()\n",
            "endpoints",
            "base.daemon.endpoints.ServiceEndpoints.from_settings",
        ),
    ],
)
def test_handle_acquisitions_follow_the_declared_module_and_import_alias(
    make_repo: RepoFactory, source: str, kind: str, target: str
) -> None:
    root = make_repo({**FILES, "app/mod.py": source})
    assert [(a.kind.value, a.target) for a in analysis_of(root).scans["app/mod.py"].ambient] == [
        (kind, target)
    ]
    assert [d.debt.key for d in diagnostics(root, "ambient-read") if d.debt] == [
        f"mod.py#{kind} {target}"
    ]


@pytest.mark.parametrize(
    "source",
    [
        "from base.db import Database\nx = Database\n",
        "from base.db import Database\nx = Database()\n",
        "from base.db import Database\nx = Database.from_boot(None)\n",
        "from base.db import Database\ndb = Database.from_boot(None)\nx = db.query()\n",
        "class Database:\n    def from_settings(): ...\n\nx = Database.from_settings()\n",
        "from base.events.live import EventBus\nx = EventBus.from_boot(None)\n",
        "from base.daemon.endpoints import ServiceEndpoints as Table\nx = Table.from_boot(None)\n",
    ],
)
def test_importing_a_handle_type_or_building_it_from_explicit_inputs_is_not_an_acquisition(
    make_repo: RepoFactory, source: str
) -> None:
    root = make_repo({**FILES, "app/mod.py": source})
    assert analysis_of(root).scans["app/mod.py"].ambient == []
    assert diagnostics(root, "ambient-read") == []


@pytest.mark.parametrize("root_kind", ["db", "bus", "endpoints"])
def test_a_handle_root_exempts_its_own_kind_and_keeps_the_other_handles_visible(
    make_repo: RepoFactory, root_kind: str
) -> None:
    root = make_repo(
        {
            **FILES,
            "app/mod.py": (
                "from base.db import Database\n"
                "from base.events.live import EventBus\n"
                "from base.daemon.endpoints import ServiceEndpoints\n"
                "db = Database.from_settings()\nbus = EventBus.from_settings()\n"
                "endpoints = ServiceEndpoints.from_settings()\n"
            ),
            "app/meizel.toml": f'[roots]\n"mod.py" = {{ kinds = ["{root_kind}"], why = "reads it" }}\n',
        }
    )
    targets = [
        ("db", "base.db.Database.from_settings"),
        ("bus", "base.events.live.EventBus.from_settings"),
        ("endpoints", "base.daemon.endpoints.ServiceEndpoints.from_settings"),
    ]
    assert [d.debt.key for d in diagnostics(root, "ambient-read") if d.debt] == [
        f"mod.py#{kind} {target}" for kind, target in targets if kind != root_kind
    ]

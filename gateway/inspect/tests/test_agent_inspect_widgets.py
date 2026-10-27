# meiva:note
# Integration tests for GET /api/agents/{id}/inspect/widgets (task #2909;
# taskList payload reshaped in #3216).
#
# The extension surface where enabled plugins embed inspector widgets: the
# gateway builds the widget registry in process (`load_inspect_widgets`), resolves each widget's payload for the agent — a
# `taskList` lists the agent's active tasks — every one — and drops
# widgets with nothing to show.
#
# Locks: empty registry -> [], unknown agent -> 404, the taskList resolution
# rules (owner filter, active statuses only, priority-first order — P0..P3,
# ties by id — each row's own priority carried through, no cap, empty payload
# -> widget dropped), widget
# ordering/attribution passthrough, the loader's enabled-set filtering, and its
# fail-soft skip of a broken inspector.py.

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException
from psycopg_pool import ConnectionPool

import meiva_builtins.plugins as shipped_plugins
from base.db import Database
from base.packages.plugins.data_registry import DeclaredFace, build_data_registry
from base.packages.plugins.extensions import PluginContributions
from base.packages.plugins.surface.inspector import DuplicateInspectWidget, InspectWidgetSpec
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.inspect import _plugin_widgets
from gateway.inspect._plugin_faces import SHIPPED_PLUGINS_DIR, enabled_plugin_names


@pytest.fixture
def pool(database: Database) -> Iterator[ConnectionPool]:
    """A real isolated database, borrowed the way the route borrows it."""
    with database.pool(max_size=2) as opened:
        yield opened


def _widget(**over: Any) -> InspectWidgetSpec:
    data: dict[str, Any] = {
        "id": "today-tasks",
        "kind": "taskList",
        "order": 250,
    }
    data.update(over)
    face = DeclaredFace(
        "meiva_fleet", PluginContributions(inspect_widgets=(InspectWidgetSpec(**data),))
    )
    registry, refused = build_data_registry([face])
    assert refused == []
    return next(iter(registry.inspect_widgets()))


def _insert_agent(db: psycopg.Connection, label: str = "t") -> int:
    """INSERT an agents row + its agents_meta row (the /inspect family checks
    agents_meta; a bare `agents` row would 404)."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    tid = row[0]
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (tid,),
        )
    return tid


def _root_task_id(db: psycopg.Connection) -> int:
    """The system root's id for use as a parent (the throwaway test DB may or
    may not carry the seeded root row)."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM agent_tasks WHERE is_root = TRUE LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
                "VALUES ('Root', 'd', 'in_progress', 'system', TRUE) RETURNING id"
            )
            row = cur.fetchone()
    assert row is not None
    return row[0]


def _insert_task(
    db: psycopg.Connection,
    *,
    owner: int | None,
    parent_id: int | None,
    title: str,
    status: str = "in_progress",
    updated_seconds_ago: float = 0,
    priority: str = "P2",
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks "
            "(parent_id, title, description, status, owner, created_by, updated_at, priority) "
            "VALUES (%s, %s, 'd', %s, %s, %s, now() - make_interval(secs => %s), %s) RETURNING id",
            (
                parent_id,
                title,
                status,
                owner,
                str(owner) if owner is not None else "user",
                updated_seconds_ago,
                priority,
            ),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


# meiva:note
# The inspector widgets of `aid` for these registry rows, as the route serializes them.
def _get(pool: ConnectionPool, aid: int, *specs: InspectWidgetSpec) -> list[dict[str, Any]]:
    results = _plugin_widgets.widgets_for_agent(pool, aid, specs=specs)
    return [result.model_dump(mode="json") for result in results]


# ── empty registry / unknown agent ────────────────────────────────────────────


def test_empty_registry_returns_empty(pool: ConnectionPool, db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()
    assert _get(pool, aid) == []


def test_unknown_agent_404(pool: ConnectionPool, db_conn: psycopg.Connection) -> None:
    """No agents_meta row -> 404 (same contract as /inspect)."""
    db_conn.commit()
    with pytest.raises(HTTPException) as caught:
        _get(pool, 999999, _widget())
    assert caught.value.status_code == 404


# ── taskList resolution ───────────────────────────────────────────────────────


def test_lists_only_the_agents_active_tasks(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn, label="other")
    root = _root_task_id(db_conn)
    older = _insert_task(db_conn, owner=aid, parent_id=root, title="older", updated_seconds_ago=600)
    newer = _insert_task(db_conn, owner=aid, parent_id=root, title="newer")
    mid = _insert_task(
        db_conn,
        owner=aid,
        parent_id=root,
        title="mid",
        status="in_progress",
        updated_seconds_ago=60,
    )
    # Not active / not ours: none of these may appear.
    for title, status, owner in (
        ("done", "done", aid),
        ("cancelled", "cancelled", aid),
        ("theirs", "in_progress", other),
    ):
        _insert_task(db_conn, owner=owner, parent_id=root, title=title, status=status)
    db_conn.commit()

    body = _get(pool, aid, _widget())
    assert len(body) == 1
    widget = body[0]
    assert (widget["plugin"], widget["id"], widget["kind"], widget["order"]) == (
        "meiva_fleet",
        "today-tasks",
        "taskList",
        250,
    )
    # One rung (the default P2): id ascending — recency is no order key.
    assert [t["id"] for t in widget["tasks"]] == [older, newer, mid]
    assert widget["tasks"][0] == {"id": older, "title": "older", "priority": "P2"}


def test_task_list_shows_every_active_task(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    aid = _insert_agent(db_conn)
    root = _root_task_id(db_conn)
    # Regression (user ruling 2026-09-17): the Inspector shows EVERY active
    # task — the former kernel-side display cap of 8 is gone.
    ids = [
        _insert_task(db_conn, owner=aid, parent_id=root, title=f"t{i}", updated_seconds_ago=i)
        for i in range(11)
    ]
    db_conn.commit()

    tasks = _get(pool, aid, _widget())[0]["tasks"]
    assert len(tasks) == 11
    # One rung (the default P2): id ascending — the complete list, no cap.
    assert [t["id"] for t in tasks] == ids


def test_task_rows_carry_their_own_priority(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Each taskList row carries its own P0..P3 rung (task #3819) and the rows
    come rung-first, not newest-first (task #3866, user request 2026-09-17:
    the panel's order must be legible) — a mixed-priority queue sorts by rung,
    ties by id, regardless of recency."""
    aid = _insert_agent(db_conn)
    root = _root_task_id(db_conn)
    # Inserted recency-first on purpose: the newest row is the LOWEST rung, so
    # an updated_at-driven order would fail this test.
    p3 = _insert_task(db_conn, owner=aid, parent_id=root, title="later", priority="P3")
    p1 = _insert_task(
        db_conn, owner=aid, parent_id=root, title="next", priority="P1", updated_seconds_ago=60
    )
    p1b = _insert_task(
        db_conn, owner=aid, parent_id=root, title="next-b", priority="P1", updated_seconds_ago=30
    )
    p0 = _insert_task(
        db_conn, owner=aid, parent_id=root, title="urgent", priority="P0", updated_seconds_ago=120
    )
    db_conn.commit()

    tasks = _get(pool, aid, _widget())[0]["tasks"]
    # P0 first, then the P1 pair in id order — p1b ("next-b") is the more
    # RECENT update but carries the later id, so a recency tie-break would
    # flip them; then the newest-row P3.
    assert [(row["id"], row["priority"]) for row in tasks] == [
        (p0, "P0"),
        (p1, "P1"),
        (p1b, "P1"),
        (p3, "P3"),
    ]


def test_widget_without_tasks_is_dropped(pool: ConnectionPool, db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()
    assert _get(pool, aid, _widget()) == []


def test_widgets_keep_registration_order(pool: ConnectionPool, db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    root = _root_task_id(db_conn)
    _insert_task(db_conn, owner=aid, parent_id=root, title="work")
    db_conn.commit()

    body = _get(pool, aid, _widget(id="second", order=750), _widget(id="first", order=10))
    assert [w["id"] for w in body] == ["second", "first"]
    assert [w["order"] for w in body] == [750, 10]


def _widget_source(widget_id: str) -> str:
    """An `inspector.py` body declaring one taskList widget."""
    return (
        "from base.packages.plugins.extensions import PluginContributions\n"
        "from base.packages.plugins.surface.inspector import InspectWidgetSpec\n"
        f"_W = InspectWidgetSpec(id={widget_id!r}, kind='taskList', order=50)\n"
        "WIDGETS = (_W,)\n"
        "def contribute():\n"
        "    return PluginContributions(inspect_widgets=WIDGETS)\n"
    )


@pytest.fixture(autouse=True)
def _fixture_plugins_importable(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """Let the loader (which imports by dotted name) find fixture plugins written under `tmp_path`,
    and forget their modules afterwards."""
    shipped_plugins.__path__.append(str(tmp_path))

    def _forget() -> None:
        shipped_plugins.__path__.remove(str(tmp_path))
        for name in [m for m in sys.modules if m.startswith("meiva_builtins.plugins.fx_")]:
            del sys.modules[name]

    request.addfinalizer(_forget)


def _fixture_inspector(root: Path, plugin: str, source: str, *, widget_id: str) -> Path:
    """Write `<root>/<plugin>/inspector.py` (a package, so `meiva_builtins.plugins.<plugin>.inspector`
    imports)."""
    plugin_dir = root / plugin
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": plugin,
                "version": "1.0.0",
                "engines": {"meiva": ">=2026.10.1"},
                "contributions": {"inspectWidgets": [widget_id]},
            }
        ),
        encoding="utf-8",
    )
    inspector_py = plugin_dir / "inspector.py"
    inspector_py.write_text(source, encoding="utf-8")
    return inspector_py


# ── the loader ────────────────────────────────────────────────────────────────


def test_loader_imports_shipped_fleet_widget() -> None:
    assert (SHIPPED_PLUGINS_DIR / "meiva_fleet" / "inspector.py").is_file()

    specs = _plugin_widgets.load_inspect_widgets(SHIPPED_PLUGINS_DIR, {"meiva_fleet"})
    assert [(s.plugin, s.id, s.kind, s.order) for s in specs] == [
        ("meiva_fleet", "today-tasks", "taskList", 250)
    ]
    assert specs[0].title is None


def test_loader_filters_widgets_of_disabled_plugins() -> None:
    """A plugin disabled after its module was imported (and served) must not
    serve widgets: the registry is rebuilt from the enabled set on every call."""
    assert [
        s.id for s in _plugin_widgets.load_inspect_widgets(SHIPPED_PLUGINS_DIR, {"meiva_fleet"})
    ] == ["today-tasks"]
    assert _plugin_widgets.load_inspect_widgets(SHIPPED_PLUGINS_DIR, set()) == []


# meiva:note
# `enabled_inspector_modules` follows the enable bit the machine's plugin config holds,
# read per request.
def test_enabled_modules_skips_a_disabled_plugin(unit_home: Path) -> None:
    module = SHIPPED_PLUGINS_DIR / "meiva_fleet" / "inspector.py"
    assert module in _plugin_widgets.enabled_inspector_modules(
        SHIPPED_PLUGINS_DIR, enabled_plugin_names(home=unit_home)
    )

    (unit_home / "plugins_config.json").write_text(
        json.dumps({"plugins": {"meiva_fleet": {"enabled": False}}}), encoding="utf-8"
    )
    assert module not in _plugin_widgets.enabled_inspector_modules(
        SHIPPED_PLUGINS_DIR, enabled_plugin_names(home=unit_home)
    )


def test_loader_skips_a_plugin_whose_inspector_fails_to_import(
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
    emitted_events: EmittedEvents,
) -> None:
    """Fail-soft (user ruling 2026-09-11): a broken inspector.py is reported
    loudly and skipped — the endpoint keeps serving the remaining widgets.

    The 2026-08-28 meiva_ledger incident shape (a sibling import that blows up
    at module import) restated for the inspector surface: the load must not
    raise, the remaining plugin still serves, and the failure is loud on both
    channels (loguru ERROR + the plugin_load_failed telemetry event)."""
    _fixture_inspector(tmp_path, "fx_fine", _widget_source("fine"), widget_id="fine")
    _fixture_inspector(
        tmp_path,
        "fx_broken_plugin",
        "import _missing_for_the_test\n" + _widget_source("w"),
        widget_id="w",
    )

    # A failed import must not leave the module behind (importlib's own cleanup).
    leftover = "meiva_builtins.plugins.fx_broken_plugin.inspector"
    specs = _plugin_widgets.load_inspect_widgets(tmp_path, {"fx_fine", "fx_broken_plugin"})
    assert leftover not in sys.modules

    # the healthy plugin still serves; the broken one contributes nothing
    assert [(s.plugin, s.id) for s in specs] == [("fx_fine", "fine")]
    # loud: a loguru error naming the plugin
    assert any(
        "fx_broken_plugin" in r["message"] and "fail-soft" in r["message"] for r in loguru_records
    )
    # loud: the plugin_load_failed event carrying the plugin + the exception
    attrs = emitted_events.attributes("plugin_load_failed")
    assert [a["plugin"] for a in attrs] == ["fx_broken_plugin"]
    assert "ModuleNotFoundError" in str(attrs[0]["error"])

    # every plugin broken -> still no raise, an empty registry
    assert _plugin_widgets.load_inspect_widgets(tmp_path, {"fx_broken_plugin"}) == []


def test_loader_skips_a_failing_inspector_and_recovers(
    tmp_path: Path, loguru_records: list[dict[str, Any]]
) -> None:
    """An inspector.py that raises contributes nothing: a declaration is
    admitted whole or not at all, so a fixed file recovers on the next request
    (fail-soft, user ruling 2026-09-11)."""
    inspector_py = _fixture_inspector(
        tmp_path,
        "fx_drop_partial_insp",
        _widget_source("drop_partial_widget") + "raise RuntimeError('inspector boom')\n",
        widget_id="drop_partial_widget",
    )
    enabled = {"fx_drop_partial_insp"}

    assert _plugin_widgets.load_inspect_widgets(tmp_path, enabled) == []  # must not raise
    assert any(
        "fx_drop_partial_insp" in r["message"] and "failed to load" in r["message"]
        for r in loguru_records
    )

    inspector_py.write_text(_widget_source("drop_partial_widget"), encoding="utf-8")
    specs = _plugin_widgets.load_inspect_widgets(tmp_path, enabled)
    assert [(s.plugin, s.id) for s in specs] == [("fx_drop_partial_insp", "drop_partial_widget")]


def test_loader_refuses_a_plugin_declaring_a_widget_id_twice(
    tmp_path: Path, loguru_records: list[dict[str, Any]]
) -> None:
    """A widget id repeated within one plugin refuses that plugin whole (none
    of its widgets serve); another plugin's widgets are untouched."""
    _fixture_inspector(
        tmp_path,
        "fx_twice",
        _widget_source("same") + "WIDGETS = (_W, _W)\n",
        widget_id="same",
    )
    _fixture_inspector(tmp_path, "fx_fine", _widget_source("fine"), widget_id="fine")

    specs = _plugin_widgets.load_inspect_widgets(tmp_path, {"fx_twice", "fx_fine"})
    assert [(s.plugin, s.id) for s in specs] == [("fx_fine", "fine")]
    assert any(
        "fx_twice" in r["message"]
        and r["exception"] is not None
        and isinstance(r["exception"].value, DuplicateInspectWidget)
        for r in loguru_records
    )


def test_widget_plugin_comes_from_the_registry_entry() -> None:
    """A spec claiming another plugin is attributed to the plugin that declared it."""
    face = DeclaredFace(
        "meiva_fleet",
        PluginContributions(
            inspect_widgets=(
                InspectWidgetSpec(id="w", kind="taskList", order=1, plugin="someone_else"),
            )
        ),
    )
    registry, _refused = build_data_registry([face])
    assert [(s.plugin, s.id) for s in registry.inspect_widgets()] == [("meiva_fleet", "w")]

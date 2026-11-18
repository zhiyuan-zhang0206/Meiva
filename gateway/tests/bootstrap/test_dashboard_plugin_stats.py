# meiva:note
# Integration tests for the unwindowed `plugin_stats` cards on GET /api/stats/dashboard:
# declaration filtering against `contributions.ui.stats` / `statsFace`, invalid manifest failure,
# and retained-error snapshot freshness.
# meiva:link gateway/cluster/docs/ops-surfaces.md

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from base import paths
from base.db import Database
from base.packages.plugin_config_images import config_image_path
from base.packages.plugins.enable_config import write_local
from base.packages.plugins.surface import stats
from base.packages.plugins.surface.stats_types import StatReset
from gateway.tests.auth.gateway_config import gateway_test_client
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.runner import refresh_collector_once


# meiva:note
# An enabled plugin that declares `cards` under `contributions.ui.stats`: the declaration the
# dashboard now keeps a `plugin_stats` row against.
def _declare_stat_cards(home: Path, plugin: str, cards: dict[str, str]) -> None:
    directory = paths.plugins_dir(home=home) / plugin
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text('"""A test plugin."""\n', encoding="utf-8")
    manifest = {
        "apiVersion": 2,
        "name": plugin.replace("_", "-"),
        "version": "1.0.0",
        "engines": {"meiva": ">=0.1.0"},
        "contributions": {
            "ui": {"stats": [{"id": id, "label": label} for id, label in cards.items()]}
        },
    }
    (directory / "meiva-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    write_local({"plugins": {plugin: {"enabled": True}}}, home=home)


# meiva:note
# The builtin `meiva_usage` plugin enabled with these collectors: its `statsFace` derives the
# declared cards from them, which is the path the real status cards take.
def _enable_usage_collectors(home: Path, *collectors: CollectorConfig) -> None:
    image = config_image_path("meiva_usage", home=home)
    image.parent.mkdir(parents=True)
    image.write_text(UsageConfig(collectors=collectors).model_dump_json(), encoding="utf-8")
    write_local({"plugins": {"meiva_usage": {"enabled": True}}}, home=home)


# meiva:note
# The runtime half of collected plugin cards rides this response: every row of a declared card, its
# status, and its freshness metadata. Not windowed — the plugin's value is a point-in-time fact and
# the window selector must not pretend to aggregate it. The console joins declarations from
# /api/ui/contributions on (plugin, id).
def test_dashboard_carries_plugin_stat_values_unwindowed(
    database: Database, unit_home: Path
) -> None:
    _declare_stat_cards(
        unit_home,
        "codex_usage",
        {"codex-zhang0206": "Codex zhang0206", "codex-wuji": "Codex wuji"},
    )
    stats.upsert(
        database,
        plugin="codex_usage",
        id="codex-zhang0206",
        value="6%",
        resets=(StatReset.model_validate({"label": "7d", "resets_at": "2027-01-01T00:00:00Z"}),),
        detail="94% used - weekly",
        status="warn",
        updated_by="macmini",
    )
    stats.upsert(
        database,
        plugin="codex_usage",
        id="codex-wuji",
        value="!",
        detail="token revoked",
        status="error",
        updated_by="macmini",
    )

    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard", params={"hours": 6}).json()

    rows = body["plugin_stats"]
    assert [
        (r["plugin"], r["id"], r["value"], r["detail"], r["status"], r["updated_by"]) for r in rows
    ] == [
        ("codex_usage", "codex-wuji", "!", "token revoked", "error", "macmini"),
        ("codex_usage", "codex-zhang0206", "6%", "94% used - weekly", "warn", "macmini"),
    ]
    assert all(r["updated_at"] for r in rows)
    assert rows[0]["resets"] == []
    assert rows[1]["resets"] == [{"label": "7d", "resets_at": "2027-01-01T00:00:00Z"}]


# meiva:note
# The 2026-11-02 split of an antigravity collector's one card into a Gemini and a Claude & GPT card
# left the old `antigravity` row behind. The console joined it away; `meiva stats` has no join, so
# the dashboard serves only what a declaration claims.
def test_dashboard_drops_a_pre_split_card_row_no_declaration_claims(
    database: Database, unit_home: Path
) -> None:
    _enable_usage_collectors(
        unit_home,
        CollectorConfig(
            id="antigravity", label="Antigravity", provider="antigravity", machine="gateway"
        ),
    )
    for plugin, card in (
        ("meiva_usage", "antigravity-gemini"),
        ("meiva_usage", "antigravity-claude-gpt"),
        ("meiva_usage", "antigravity"),
        ("removed_usage", "gone"),
    ):
        stats.upsert(database, plugin=plugin, id=card, value="1% left", status="warn")

    with gateway_test_client() as client:
        rows = client.get("/api/stats/dashboard").json()["plugin_stats"]

    assert [(r["plugin"], r["id"]) for r in rows] == [
        ("meiva_usage", "antigravity-claude-gpt"),
        ("meiva_usage", "antigravity-gemini"),
    ]


def test_dashboard_invalid_plugin_manifest_names_the_plugin(unit_home: Path) -> None:
    _declare_stat_cards(unit_home, "broken", {"a": "A"})
    (paths.plugins_dir(home=unit_home) / "broken" / "meiva-plugin.json").write_text("{", "utf-8")

    with gateway_test_client() as client:
        resp = client.get("/api/stats/dashboard")

    assert resp.status_code == 500
    assert "broken" in resp.text


def test_dashboard_plugin_stats_is_empty_without_writers() -> None:
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["plugin_stats"] == []


# meiva:note
# Failed probes update the warning without making retained quota/deadlines look freshly read.
def test_retained_error_snapshot_age_reaches_dashboard(
    database: Database,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    unit_home: Path,
) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    reset = StatReset(label="5h", resets_at=old + timedelta(hours=3))
    stats.upsert(
        database, plugin="meiva_usage", id="cached-quota", value="40% left", resets=(reset,)
    )
    _ = db_conn.execute("UPDATE plugin_stats SET updated_at = %s WHERE id = 'cached-quota'", (old,))
    db_conn.commit()
    collector = CollectorConfig(
        id="cached-quota", label="Cached", provider="deepseek", machine="gateway", key_env="KEY"
    )
    _enable_usage_collectors(unit_home, collector)

    def failed(_request: object) -> dict[str, object]:
        raise RuntimeError("network down")

    for elapsed_hours in (1, 2, 4):
        _ = refresh_collector_once(
            collector,
            db=database,
            user_home=tmp_path,
            environ={"KEY": "test"},
            now=old + timedelta(hours=elapsed_hours),
            platform="darwin",
            http_fetch=failed,
        )
        with gateway_test_client() as client:
            (row,) = client.get("/api/stats/dashboard").json()["plugin_stats"]
        assert datetime.fromisoformat(row["updated_at"]) == old
        assert row["value"] == "40% left"
        assert row["resets"] == [{"label": "5h", "resets_at": "2026-01-01T03:00:00Z"}]
        assert row["status"] == "error"

    def recovered(_request: object) -> dict[str, object]:
        return {"balance_infos": [{"currency": "USD", "total_balance": "99.00"}]}

    _ = refresh_collector_once(
        collector,
        db=database,
        user_home=tmp_path,
        environ={"KEY": "test"},
        now=old + timedelta(hours=5),
        platform="darwin",
        http_fetch=recovered,
    )
    with gateway_test_client() as client:
        (row,) = client.get("/api/stats/dashboard").json()["plugin_stats"]
    assert datetime.fromisoformat(row["updated_at"]) > old
    assert row["value"] == "USD 99.00"
    assert row["resets"] == []
    assert row["status"] == "ok"

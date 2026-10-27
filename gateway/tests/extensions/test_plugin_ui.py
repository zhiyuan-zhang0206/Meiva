import json
from pathlib import Path

import pytest

from base import paths
from base.packages.plugins.enable_config import write_local
from gateway.tests.auth.gateway_config import gateway_test_client


def _write_plugin(home: Path, name: str, *, files: dict[str, str] | None = None) -> Path:
    directory = paths.plugins_dir(home=home) / name
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text('"""A test plugin."""\n', encoding="utf-8")
    for rel, content in (files or {}).items():
        target = directory / "ui" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


_BOARD = "<!doctype html><title>Board</title><h1>Task board</h1>"


def test_serves_the_mount_root_index(unit_home: Path) -> None:
    _write_plugin(unit_home, "board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/plugin-ui/board/")
    assert r.status_code == 200, r.text
    assert r.text == _BOARD
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_bare_mount_redirects_to_the_trailing_slash(unit_home: Path) -> None:
    _write_plugin(unit_home, "board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/plugin-ui/board", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/api/plugin-ui/board/"


def test_serves_a_subdirectory_index_and_a_nested_asset(unit_home: Path) -> None:
    _write_plugin(
        unit_home,
        "board",
        files={"board/index.html": _BOARD, "board/app.js": "console.log(1)\n"},
    )
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        page = client.get("/api/plugin-ui/board/board/")
        asset = client.get("/api/plugin-ui/board/board/app.js")
    assert page.status_code == 200, page.text
    assert page.text == _BOARD
    assert asset.status_code == 200, asset.text
    assert asset.text == "console.log(1)\n"
    assert asset.headers["content-type"].startswith("text/javascript")


def test_directory_without_a_trailing_slash_redirects(unit_home: Path) -> None:
    _write_plugin(unit_home, "board", files={"board/index.html": _BOARD, "board/app.js": "1\n"})
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/plugin-ui/board/board", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/api/plugin-ui/board/board/"


def test_dash_spelled_plugin_name_reaches_the_underscore_directory(unit_home: Path) -> None:
    _write_plugin(unit_home, "meiva_board", files={"index.html": _BOARD})
    write_local({"plugins": {"meiva_board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/plugin-ui/meiva-board/")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["", "index.html"])
def test_disabled_plugin_has_no_mount(path: str, unit_home: Path) -> None:
    _write_plugin(unit_home, "board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": False}}})

    with gateway_test_client() as client:
        r = client.get(f"/api/plugin-ui/board/{path}")
    assert r.status_code == 404, r.text


def test_unknown_plugin_and_a_plugin_without_pages_answer_alike(unit_home: Path) -> None:
    _write_plugin(unit_home, "plain")
    write_local({"plugins": {"plain": {"enabled": True}}})

    with gateway_test_client() as client:
        missing = client.get("/api/plugin-ui/nope/")
        no_pages = client.get("/api/plugin-ui/plain/")
    assert missing.status_code == 404
    assert no_pages.status_code == 404
    assert missing.json()["detail"] == "no plugin page mount for 'nope'"
    assert no_pages.json()["detail"] == "no plugin page mount for 'plain'"


@pytest.mark.parametrize("rest", ["%2e%2e/plugin.py", "board/%2e%2e/%2e%2e/plugin.py", "a\\b"])
def test_traversal_segments_are_refused(rest: str, unit_home: Path) -> None:
    _write_plugin(unit_home, "board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get(f"/api/plugin-ui/board/{rest}")
    assert r.status_code == 400, r.text
    assert "invalid page path segment" in r.text


def test_a_symlink_out_of_the_ui_dir_is_refused(unit_home: Path) -> None:
    directory = _write_plugin(unit_home, "board", files={"index.html": _BOARD})
    secret = directory.parent.parent / "secret.txt"
    secret.write_text("cluster secret\n", encoding="utf-8")
    (directory / "ui" / "escape.txt").symlink_to(secret)
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/plugin-ui/board/escape.txt")
    assert r.status_code == 404, r.text
    assert "cluster secret" not in r.text


def test_missing_file_is_404(unit_home: Path) -> None:
    _write_plugin(unit_home, "board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/plugin-ui/board/nope.html")
    assert r.status_code == 404, r.text


def test_nav_declarations_reach_the_console_attributed(unit_home: Path) -> None:
    directory = _write_plugin(unit_home, "board", files={"board/index.html": _BOARD})
    (directory / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "board",
                "version": "1.0.0",
                "engines": {"meiva": ">=0.1.0"},
                "contributions": {
                    "ui": {
                        "nav": [
                            {
                                "location": "sidebar",
                                "label": "Task board",
                                "icon": "kanban",
                                "page": "board/",
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    write_local({"plugins": {"board": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/ui/contributions")
    assert r.status_code == 200, r.text
    assert r.json()["nav"] == [
        {
            "plugin": "board",
            "location": "sidebar",
            "label": "Task board",
            "icon": "kanban",
            "page": "board/",
        }
    ]

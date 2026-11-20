# meiva:note
# The workspace viewer's tree and quick-open as the browser sees them: one directory at a time
# under the same roots and refusals as a file read, a bounded name search, and a forwarded raw
# read that answers byte ranges so media can seek. Reads on another machine go through a real
# HTTP listener answering as the owning runner does.
# meiva:link gateway/agents/docs/to-user-resources.md

import json
from collections.abc import Iterator
from pathlib import Path
from typing import override

import psycopg
import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.db import create_agent
from base.paths import workspace_path
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.support.runner_ops.workspace_ops import (
    register_runner,
    remote_workspace_ops_server,
)
from ops.to_user import list_workspace_dir, search_workspace_files


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    with gateway_test_client() as value:
        yield value


def _agent_on(conn: psycopg.Connection, machine: str) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status) VALUES (%s, %s, 'idling')",
        (agent, machine),
    )
    conn.commit()
    return agent


def _tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)


def test_listing_puts_directories_first_then_files_by_name(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent_on(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent)).resolve()
    _tree(ws, {"b_dir/x.txt": b"x", "A_dir/inner.md": b"# inner", "z.txt": b"zz", "a.md": b"a"})
    (ws / "empty").mkdir()

    root = client.get(f"/api/agents/{agent}/files/list")
    assert root.status_code == 200
    body = root.json()
    assert (body["path"], body["parent"], body["truncated"]) == (str(ws), None, False)
    assert [(e["name"], e["kind"], e["size"]) for e in body["entries"]] == [
        ("A_dir", "dir", None),
        ("b_dir", "dir", None),
        ("empty", "dir", None),
        ("a.md", "file", 1),
        ("z.txt", "file", 2),
    ]
    assert [e["path"] for e in body["entries"]] == [str(ws / e["name"]) for e in body["entries"]]


def test_listing_walks_down_by_absolute_or_relative_path(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent_on(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent)).resolve()
    _tree(ws, {"A_dir/inner.md": b"# inner", "a.md": b"a"})

    child = client.get(f"/api/agents/{agent}/files/list", params={"path": str(ws / "A_dir")})
    assert (child.status_code, child.json()["parent"]) == (200, str(ws))
    assert [e["name"] for e in child.json()["entries"]] == ["inner.md"]

    relative = client.get(f"/api/agents/{agent}/files/list?path=A_dir")
    assert relative.json()["path"] == str(ws / "A_dir")

    for not_a_directory in ("a.md", "missing"):
        listing = client.get(f"/api/agents/{agent}/files/list", params={"path": not_a_directory})
        assert listing.status_code == 404


def test_listing_is_capped_and_says_so(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws = workspace_path(home, 7)
    _tree(ws, {"c.txt": b"", "d.txt": b"", "b/x": b"", "a/x": b""})

    listing = list_workspace_dir(home, 7, "", max_entries=3)
    assert [e.name for e in listing.entries] == ["a", "b", "c.txt"]
    assert listing.truncated is True


def test_listing_refuses_escapes_and_outward_symlinks(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _agent_on(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    _tree(ws, {"inside/kept.txt": b"k"})
    outside = tmp_path / "outside"
    _tree(outside, {"secret.txt": b"s"})
    (ws / "escape").symlink_to(outside, target_is_directory=True)
    (ws / "escape.txt").symlink_to(outside / "secret.txt")
    (ws / "alias").symlink_to(ws / "inside", target_is_directory=True)

    for forbidden in (
        "..",
        "../..",
        "/etc",
        str(outside),
        "escape",
        f"/Users/other/.meiva/workspaces/{agent}/..",
        f"/Users/other/.meiva/workspaces/{agent}/../{agent + 1}",
    ):
        response = client.get(f"/api/agents/{agent}/files/list", params={"path": forbidden})
        assert response.status_code == 403, forbidden

    names = [e["name"] for e in client.get(f"/api/agents/{agent}/files/list").json()["entries"]]
    assert names == ["alias", "inside"]


def test_listing_names_another_agents_workspace(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    viewer = _agent_on(db_conn, "macmini")
    owner = _agent_on(db_conn, "macmini")
    Path(workspace_path(client.app.state.gateway_process.home, viewer)).mkdir(parents=True)
    ws_owner = Path(workspace_path(client.app.state.gateway_process.home, owner))
    _tree(ws_owner, {"deliver/report.md": b"# r"})

    root = client.get(
        f"/api/agents/{viewer}/files/list",
        params={"path": f"/Users/other/.meiva/workspaces/{owner}"},
    )
    assert root.status_code == 200
    assert root.json()["path"] == str(ws_owner.resolve())
    assert root.json()["parent"] is None
    assert [e["name"] for e in root.json()["entries"]] == ["deliver"]

    deliver = client.get(
        f"/api/agents/{viewer}/files/list",
        params={"path": f"/Users/other/.meiva/workspaces/{owner}/deliver"},
    )
    assert deliver.status_code == 200
    assert deliver.json()["parent"] == str(ws_owner.resolve())


def test_listing_climbs_only_inside_the_custom_cwd(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _agent_on(db_conn, "macmini")
    Path(workspace_path(client.app.state.gateway_process.home, agent)).mkdir(parents=True)
    project = tmp_path / "project"
    _tree(project, {"src/app.py": b"print()"})
    ckpt = json.dumps({"channel_values": {"meiva_code__cwd": str(project)}})
    _ = db_conn.execute(
        """
        INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)
        VALUES (%s, '', '1', 'json', %s::jsonb, '{}'::jsonb)
        """,
        (str(agent), ckpt),
    )
    db_conn.commit()

    src = client.get(f"/api/agents/{agent}/files/list?path=src")
    assert src.status_code == 200
    assert src.json()["parent"] == str(project.resolve())
    top = client.get(f"/api/agents/{agent}/files/list", params={"path": str(project)})
    assert top.status_code == 200
    assert top.json()["parent"] is None


_SEARCH_TREE = {
    "src/components/FileTree.tsx": b"",
    "src/file_tree_utils.py": b"",
    "docs/tree.md": b"",
    "fileTREE.md": b"",
    ".env": b"",
    "node_modules/pkg/filetree.js": b"",
    "__pycache__/filetree.pyc": b"",
    ".git/filetree": b"",
    ".venv/lib/filetree.py": b"",
    ".next/filetree.js": b"",
    ".github/filetree.yml": b"",
}


def test_search_ranks_substring_hits_before_subsequence_hits(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent_on(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    _tree(ws, _SEARCH_TREE)

    response = client.get(f"/api/agents/{agent}/files/search", params={"q": "FileTree"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "FileTree"
    assert body["truncated"] is False
    assert [hit["relative"] for hit in body["results"]] == [
        "fileTREE.md",
        "src/components/FileTree.tsx",
        "src/file_tree_utils.py",
    ]
    first = body["results"][0]
    assert first["name"] == "fileTREE.md"
    assert first["path"] == str(ws.resolve() / "fileTREE.md")

    dotfile = client.get(f"/api/agents/{agent}/files/search", params={"q": ".env"})
    assert [hit["relative"] for hit in dotfile.json()["results"]] == [".env"]

    limited = client.get(f"/api/agents/{agent}/files/search", params={"q": "filetree", "limit": 1})
    assert [hit["relative"] for hit in limited.json()["results"]] == ["fileTREE.md"]
    assert limited.json()["truncated"] is True

    scoped = client.get(f"/api/agents/{agent}/files/search", params={"q": "tree", "path": "docs"})
    assert [hit["relative"] for hit in scoped.json()["results"]] == ["tree.md"]

    assert client.get(f"/api/agents/{agent}/files/search").status_code == 422
    assert client.get(f"/api/agents/{agent}/files/search?q=").status_code == 422
    assert client.get(f"/api/agents/{agent}/files/search?q=x&limit=0").status_code == 422


def test_search_never_leaves_its_root(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _agent_on(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent))
    _tree(ws, {"kept_secret.txt": b""})
    outside = tmp_path / "outside"
    _tree(outside, {"other_secret.txt": b""})
    (ws / "escape").symlink_to(outside, target_is_directory=True)
    (ws / "escape_secret.txt").symlink_to(outside / "other_secret.txt")
    (ws / "loop").symlink_to(ws, target_is_directory=True)

    found = client.get(f"/api/agents/{agent}/files/search", params={"q": "secret"})
    assert [hit["relative"] for hit in found.json()["results"]] == ["kept_secret.txt"]

    for forbidden in ("..", "/etc", str(outside), "escape"):
        response = client.get(
            f"/api/agents/{agent}/files/search", params={"q": "secret", "path": forbidden}
        )
        assert response.status_code == 403, forbidden


# meiva:note
# Every reading of these timers is a second later than the last, so the walk's deadline passes
# after the first directories it opens.
class _SecondPerReading(ManualTimers):
    @override
    def monotonic(self) -> float:
        self.advance(1.0)
        return super().monotonic()


def test_search_stops_at_its_file_and_time_bounds(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws = workspace_path(home, 7)
    _tree(ws, {f"d{depth}/" * depth + f"note{depth}.md": b"" for depth in range(1, 6)})
    _tree(ws, {"note0.md": b""})

    whole = search_workspace_files(home, 7, "", "note", limit=50, timers=ManualTimers())
    assert len(whole.results) == 6
    assert whole.truncated is False

    by_count = search_workspace_files(
        home, 7, "", "note", limit=50, timers=ManualTimers(), max_files=2
    )
    assert len(by_count.results) == 2
    assert by_count.truncated is True

    by_time = search_workspace_files(home, 7, "", "note", limit=50, timers=_SecondPerReading())
    assert by_time.truncated is True
    assert len(by_time.results) < 6


_CLIP = bytes(range(256)) * 4
_CLIP_SIZE = len(_CLIP)


@pytest.fixture
def remote_owner(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> Iterator[tuple[int, Path]]:
    owner = _agent_on(db_conn, "company-mini")
    remote_home = tmp_path / "company-mini-home"
    _tree(
        workspace_path(remote_home, owner),
        {"media/clip.mp4": _CLIP, "media/talk.mp3": b"ID3", "notes.md": b"# n"},
    )
    with remote_workspace_ops_server(remote_home, owner) as ops_url:
        register_runner(db_conn, "company-mini", ops_url)
        yield owner, workspace_path(remote_home, owner)


def test_forwarded_listing_and_search(
    client: GatewayTestClient, db_conn: psycopg.Connection, remote_owner: tuple[int, Path]
) -> None:
    owner, ws_remote = remote_owner
    ws_remote = ws_remote.resolve()
    viewer = _agent_on(db_conn, "macmini")
    Path(workspace_path(client.app.state.gateway_process.home, viewer)).mkdir(parents=True)

    own_root = client.get(f"/api/agents/{owner}/files/list").json()
    assert own_root["path"] == str(ws_remote)
    assert [e["name"] for e in own_root["entries"]] == ["media", "notes.md"]

    listing = client.get(
        f"/api/agents/{viewer}/files/list",
        params={"path": f"/Users/zhiyuan-output/.meiva/workspaces/{owner}/media"},
    ).json()
    assert listing["parent"] == str(ws_remote)
    assert [e["name"] for e in listing["entries"]] == ["clip.mp4", "talk.mp3"]

    refused = client.get(f"/api/agents/{owner}/files/list", params={"path": "../.."})
    assert refused.status_code == 403

    found = client.get(f"/api/agents/{owner}/files/search", params={"q": "clp"}).json()
    assert [(hit["relative"], hit["path"]) for hit in found["results"]] == [
        ("media/clip.mp4", str(ws_remote / "media" / "clip.mp4"))
    ]


# meiva:note
# A media element asks for byte ranges; the forwarded read must answer them as the local
# `FileResponse` does, or Safari plays nothing and no browser can seek. A request without a
# range, or with several ranges (which HTTP lets a server ignore), gets the whole file.
@pytest.mark.parametrize(
    ("range_header", "status", "content", "content_range"),
    [
        ("bytes=0-9", 206, _CLIP[:10], f"bytes 0-9/{_CLIP_SIZE}"),
        ("bytes=-4", 206, _CLIP[-4:], f"bytes {_CLIP_SIZE - 4}-{_CLIP_SIZE - 1}/{_CLIP_SIZE}"),
        ("bytes=1000-", 206, _CLIP[1000:], f"bytes 1000-{_CLIP_SIZE - 1}/{_CLIP_SIZE}"),
        (f"bytes={_CLIP_SIZE}-", 416, b"", f"bytes */{_CLIP_SIZE}"),
        (None, 200, _CLIP, None),
        ("bytes=0-1,4-5", 200, _CLIP, None),
    ],
)
def test_forwarded_raw_reads_answer_byte_ranges(
    client: GatewayTestClient,
    remote_owner: tuple[int, Path],
    range_header: str | None,
    status: int,
    content: bytes,
    content_range: str | None,
) -> None:
    owner, _ = remote_owner
    response = client.get(
        f"/api/agents/{owner}/files/raw?path=media/clip.mp4",
        headers={} if range_header is None else {"Range": range_header},
    )
    assert (response.status_code, response.content) == (status, content)
    assert response.headers.get("content-range") == content_range
    assert response.headers["content-length"] == str(len(content))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-security-policy"].startswith("sandbox;")


def test_forwarded_audio_plays_inline(
    client: GatewayTestClient, remote_owner: tuple[int, Path]
) -> None:
    owner, _ = remote_owner
    audio = client.get(f"/api/agents/{owner}/files/raw?path=media/talk.mp3")
    assert (audio.status_code, audio.content) == (200, b"ID3")
    assert audio.headers["content-type"] == "audio/mpeg"
    assert audio.headers["content-disposition"].startswith("inline;")

# meiva:note
# The file viewer as the browser sees it: one directory at a time and one file at a time, under
# the reach the gateway's `file_access` sets (`all`: any path on the owning machine; `workspaces`:
# agent workspaces and the agent's cwd), each answer naming the root its path bar starts from, and
# a forwarded raw read that answers byte ranges so media can seek. Reads on another machine go
# through a real HTTP listener answering as the owning runner does.
# meiva:link gateway/agents/docs/to-user-resources.md

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from base.config.domains.to_user import FileAccess
from base.db import create_agent
from base.paths import workspace_path
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client, reconfigure
from gateway.tests.support.runner_ops.workspace_ops import (
    register_runner,
    remote_workspace_ops_server,
)
from ops.to_user import FileReach, list_workspace_dir


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    with gateway_test_client() as value:
        yield value


# meiva:note
# The gateway's reach and the account home its host answers `~` and the `home` root with.
def _reach(client: GatewayTestClient, access: FileAccess, user_home: Path) -> None:
    to_user = client.app.state.gateway_config.to_user
    _ = reconfigure(client, user_home=user_home, to_user=replace(to_user, file_access=access))


def _root(kind: str, path: Path | str, agent_id: int | None = None) -> dict[str, object]:
    return {"kind": kind, "path": str(path), "agent_id": agent_id}


def _agent_on(conn: psycopg.Connection, machine: str) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status) VALUES (%s, %s, 'idling')",
        (agent, machine),
    )
    conn.commit()
    return agent


# meiva:note
# An agent of the gateway's own host: the test machine is not registered, so the gateway serves
# an agent locally when its workspace exists here.
def _local_agent(client: GatewayTestClient, conn: psycopg.Connection) -> int:
    agent = _agent_on(conn, "macmini")
    workspace_path(client.app.state.gateway_process.home, agent).mkdir(parents=True, exist_ok=True)
    return agent


def _tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)


def test_listing_puts_directories_first_then_files_by_name(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    _reach(client, "workspaces", tmp_path / "account")
    agent = _agent_on(db_conn, "macmini")
    ws = Path(workspace_path(client.app.state.gateway_process.home, agent)).resolve()
    _tree(ws, {"b_dir/x.txt": b"x", "A_dir/inner.md": b"# inner", "z.txt": b"zz", "a.md": b"a"})
    (ws / "empty").mkdir()

    root = client.get(f"/api/agents/{agent}/files/list")
    assert root.status_code == 200
    body = root.json()
    assert (body["path"], body["parent"], body["truncated"]) == (str(ws), None, False)
    assert body["root"] == _root("workspace", ws, agent)
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

    reach = FileReach(home=home, user_home=tmp_path / "account", access="all")
    listing = list_workspace_dir(reach, 7, "", max_entries=3)
    assert [e.name for e in listing.entries] == ["a", "b", "c.txt"]
    assert listing.truncated is True


def test_listing_refuses_escapes_and_outward_symlinks(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    _reach(client, "workspaces", tmp_path / "account")
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
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    _reach(client, "workspaces", tmp_path / "account")
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
    _reach(client, "workspaces", tmp_path / "account")
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


# meiva:note
# `all` reach lists the account home as `~` and the filesystem root as `/`; each answer names the
# root its path bar starts from, and only `/` has no parent.
def test_all_reach_lists_the_home_and_the_machine_root(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _local_agent(client, db_conn)
    account = (tmp_path / "account").resolve()
    _tree(account, {"docs/a.md": b"a", "z.txt": b"z"})
    _tree(tmp_path / "elsewhere", {"x.txt": b"x"})
    _reach(client, "all", account)

    home = client.get(f"/api/agents/{agent}/files/list", params={"path": "~"}).json()
    assert (home["path"], home["parent"]) == (str(account), str(account.parent))
    assert home["root"] == _root("home", account)
    assert [e["name"] for e in home["entries"]] == ["docs", "z.txt"]
    docs = client.get(f"/api/agents/{agent}/files/list", params={"path": "~/docs"}).json()
    assert (docs["parent"], docs["root"]) == (str(account), _root("home", account))

    machine = client.get(f"/api/agents/{agent}/files/list", params={"path": "/"}).json()
    assert (machine["path"], machine["parent"]) == ("/", None)
    assert machine["root"] == _root("machine", "/")
    assert machine["entries"] and machine["entries"][0]["kind"] == "dir"
    elsewhere = client.get(
        f"/api/agents/{agent}/files/list", params={"path": str(tmp_path / "elsewhere")}
    ).json()
    assert elsewhere["root"] == _root("machine", "/")


# meiva:note
# `all` reach reads any regular file: in this agent's workspace, another agent's, the account
# home, or anywhere else, and through `..` out of the workspace; each answer is the file's
# absolute path and its root.
def test_all_reach_reads_any_regular_file_with_its_root(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _agent_on(db_conn, "macmini")
    owner = _agent_on(db_conn, "macmini")
    gateway_home = client.app.state.gateway_process.home
    ws = workspace_path(gateway_home, agent).resolve()
    ws_owner = workspace_path(gateway_home, owner).resolve()
    account = (tmp_path / "account").resolve()
    _tree(ws, {"mine.md": b"mine"})
    _tree(ws_owner, {"deliver/report.md": b"report"})
    _tree(account, {"notes/a.md": b"home note"})
    _tree(tmp_path / "elsewhere", {"x.txt": b"elsewhere"})
    _reach(client, "all", account)

    for path, expected, root in (
        ("mine.md", ws / "mine.md", _root("workspace", ws, agent)),
        (
            f"../{owner}/deliver/report.md",
            ws_owner / "deliver/report.md",
            _root("workspace", ws_owner, owner),
        ),
        ("~/notes/a.md", account / "notes/a.md", _root("home", account)),
        (
            str(tmp_path / "elsewhere/x.txt"),
            (tmp_path / "elsewhere/x.txt").resolve(),
            _root("machine", "/"),
        ),
    ):
        body = client.get(f"/api/agents/{agent}/files/content", params={"path": path}).json()
        assert (body["path"], body["root"]) == (str(expected), root), path
        raw = client.get(f"/api/agents/{agent}/files/raw", params={"path": path})
        assert raw.content == expected.read_bytes(), path


# meiva:note
# Reach widens where a read may go, not what it may read: a directory, a FIFO or a missing path
# is still no file to serve, and the content size limit still holds.
def test_all_reach_still_serves_only_regular_files_within_the_limits(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _local_agent(client, db_conn)
    _tree(tmp_path / "elsewhere", {"big.txt": b"a" * (1024 * 1024 + 1)})
    os.mkfifo(tmp_path / "elsewhere" / "pipe")
    _reach(client, "all", tmp_path / "account")

    for path, status in (
        (str(tmp_path / "elsewhere"), 404),
        (str(tmp_path / "elsewhere" / "pipe"), 404),
        (str(tmp_path / "missing.txt"), 404),
        (str(tmp_path / "elsewhere" / "big.txt"), 413),
    ):
        content = client.get(f"/api/agents/{agent}/files/content", params={"path": path})
        assert content.status_code == status, path
    raw = client.get(
        f"/api/agents/{agent}/files/raw", params={"path": str(tmp_path / "elsewhere" / "pipe")}
    )
    assert raw.status_code == 404
    listing = client.get(f"/api/agents/{agent}/files/list", params={"path": str(tmp_path)})
    assert "pipe" not in [e["name"] for e in listing.json()["entries"]]


# meiva:note
# `workspaces` reach refuses what `all` reaches, yet still names the root of what it serves: a
# working directory under the account home starts its path bar at `~`.
def test_workspaces_reach_refuses_the_rest_of_the_machine(
    client: GatewayTestClient, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent = _agent_on(db_conn, "macmini")
    account = (tmp_path / "account").resolve()
    project = account / "project"
    _tree(project, {"src/app.py": b"print()"})
    _tree(account, {"secret.txt": b"s"})
    Path(workspace_path(client.app.state.gateway_process.home, agent)).mkdir(parents=True)
    ckpt = json.dumps({"channel_values": {"meiva_code__cwd": str(project)}})
    _ = db_conn.execute(
        """
        INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)
        VALUES (%s, '', '1', 'json', %s::jsonb, '{}'::jsonb)
        """,
        (str(agent), ckpt),
    )
    db_conn.commit()
    _reach(client, "workspaces", account)

    for path in ("/", str(account), "..", str(account / "secret.txt")):
        listing = client.get(f"/api/agents/{agent}/files/list", params={"path": path})
        content = client.get(f"/api/agents/{agent}/files/content", params={"path": path})
        assert (listing.status_code, content.status_code) == (403, 403), path

    app = client.get(f"/api/agents/{agent}/files/content", params={"path": "src/app.py"}).json()
    assert (app["path"], app["root"]) == (str(project / "src/app.py"), _root("home", account))


_CLIP = bytes(range(256)) * 4
_CLIP_SIZE = len(_CLIP)


# meiva:note
# An agent on another machine: its Meiva home and account home exist only there, so every read of
# them is the owning runner's answer.
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
    _tree(tmp_path / "company-mini-account", {"plans/q4.md": b"# q4"})
    with remote_workspace_ops_server(
        remote_home, owner, user_home=tmp_path / "company-mini-account"
    ) as ops_url:
        register_runner(db_conn, "company-mini", ops_url)
        yield owner, workspace_path(remote_home, owner)


def test_forwarded_listing(
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
    assert listing["root"] == _root("workspace", ws_remote, owner)
    assert [e["name"] for e in listing["entries"]] == ["clip.mp4", "talk.mp3"]


# meiva:note
# The runner applies the reach the gateway sends, not its own: the same reads of the remote
# account home and filesystem root pass under `all` and are refused under `workspaces`.
def test_forwarded_reads_carry_the_gateways_reach(
    client: GatewayTestClient, remote_owner: tuple[int, Path], tmp_path: Path
) -> None:
    owner, ws_remote = remote_owner
    account = (tmp_path / "company-mini-account").resolve()
    _reach(client, "all", tmp_path / "gateway-account")

    home = client.get(f"/api/agents/{owner}/files/list", params={"path": "~"}).json()
    assert (home["path"], home["root"]) == (str(account), _root("home", account))
    plan = client.get(f"/api/agents/{owner}/files/content", params={"path": "~/plans/q4.md"})
    assert (plan.json()["path"], plan.json()["content"]) == (str(account / "plans/q4.md"), "# q4")
    machine = client.get(f"/api/agents/{owner}/files/list", params={"path": "/"}).json()
    assert (machine["root"], machine["parent"]) == (_root("machine", "/"), None)
    note = client.get(f"/api/agents/{owner}/files/content", params={"path": "notes.md"}).json()
    assert note["root"] == _root("workspace", ws_remote.resolve(), owner)

    _reach(client, "workspaces", tmp_path / "gateway-account")
    assert client.get(f"/api/agents/{owner}/files/list", params={"path": "/"}).status_code == 403
    refused = client.get(f"/api/agents/{owner}/files/content", params={"path": str(account)})
    assert refused.status_code == 403
    assert (
        client.get(f"/api/agents/{owner}/files/list", params={"path": "../.."}).status_code == 403
    )


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

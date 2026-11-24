# meiva:link gateway/agents/docs/to-user-resources.md

import os
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import psycopg
import pytest

from base.db import create_agent
from base.paths import workspace_path
from base.telemetry.tests.capture.event_capture import EmittedEvents, capturing_events
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.support.runner_ops.workspace_ops import (
    register_runner,
    remote_workspace_ops_server,
)

_CLIP = bytes(range(256)) * 40
_NEW_CLIP = bytes(reversed(_CLIP))


class Served(NamedTuple):
    client: GatewayTestClient
    url: str
    file: Path


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


def _put(workspace: Path) -> Path:
    clip = workspace / "media" / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    _ = clip.write_bytes(_CLIP)
    return clip


@pytest.fixture(params=["local", "forwarded"])
def served(
    request: pytest.FixtureRequest,
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    tmp_path: Path,
) -> Iterator[Served]:
    if request.param == "local":
        agent = _agent_on(db_conn, "macmini")
        workspace = workspace_path(client.app.state.gateway_process.home, agent)
        workspace.mkdir(parents=True, exist_ok=True)
        yield Served(client, f"/api/agents/{agent}/files/raw?path=media/clip.mp4", _put(workspace))
        return
    agent = _agent_on(db_conn, "company-mini")
    remote_home = tmp_path / "company-mini-home"
    clip = _put(workspace_path(remote_home, agent))
    with remote_workspace_ops_server(
        remote_home, agent, user_home=tmp_path / "company-mini-account"
    ) as ops_url:
        register_runner(db_conn, "company-mini", ops_url)
        yield Served(client, f"/api/agents/{agent}/files/raw?path=media/clip.mp4", clip)


def _rewrite(file: Path, content: bytes) -> None:
    _ = file.write_bytes(content)
    stat = file.stat()
    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))


def test_a_served_file_is_private_revalidated_and_tagged(served: Served) -> None:
    first = served.client.get(served.url)
    again = served.client.get(served.url)
    assert (first.status_code, first.content) == (200, _CLIP)
    assert first.headers["cache-control"] == "private, no-cache"
    assert first.headers["etag"].startswith('"') and not first.headers["etag"].startswith("W/")
    assert "GMT" in first.headers["last-modified"]
    assert first.headers["etag"] == again.headers["etag"]
    assert first.headers["last-modified"] == again.headers["last-modified"]
    assert first.headers["content-security-policy"].startswith("sandbox;")
    assert first.headers["x-content-type-options"] == "nosniff"


def test_the_tag_follows_the_files_size_and_modification_time(served: Served) -> None:
    before = served.client.get(served.url).headers["etag"]
    _rewrite(served.file, _NEW_CLIP)
    same_size = served.client.get(served.url).headers["etag"]
    _rewrite(served.file, _NEW_CLIP + b"more")
    longer = served.client.get(served.url).headers["etag"]
    assert len({before, same_size, longer}) == 3


def test_a_slice_carries_the_tag_of_the_whole_file(served: Served) -> None:
    whole = served.client.get(served.url)
    part = served.client.get(served.url, headers={"Range": "bytes=10-19"})
    assert (part.status_code, part.content) == (206, _CLIP[10:20])
    assert part.headers["etag"] == whole.headers["etag"]
    assert part.headers["last-modified"] == whole.headers["last-modified"]
    assert part.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize("range_header", [None, "bytes=0-"])
def test_a_matching_if_none_match_is_answered_not_modified(
    served: Served, range_header: str | None
) -> None:
    tag = served.client.get(served.url).headers["etag"]
    headers = {"If-None-Match": tag}
    if range_header is not None:
        headers["Range"] = range_header
    reply = served.client.get(served.url, headers=headers)
    assert (reply.status_code, reply.content) == (304, b"")
    assert reply.headers["etag"] == tag
    assert reply.headers["cache-control"] == "private, no-cache"
    assert "content-range" not in reply.headers


@pytest.mark.parametrize(
    "if_none_match", ['"stale"', 'W/"stale", "other"', ""], ids=["stale", "list", "empty"]
)
def test_a_stale_if_none_match_gets_the_file(served: Served, if_none_match: str) -> None:
    reply = served.client.get(served.url, headers={"If-None-Match": if_none_match})
    assert (reply.status_code, reply.content) == (200, _CLIP)


def test_if_none_match_accepts_a_weak_tag_a_list_and_a_star(served: Served) -> None:
    tag = served.client.get(served.url).headers["etag"]
    for offered in (f"W/{tag}", f'"other", {tag}', "*"):
        reply = served.client.get(served.url, headers={"If-None-Match": offered})
        assert reply.status_code == 304, offered


def test_a_changed_file_is_no_longer_not_modified(served: Served) -> None:
    tag = served.client.get(served.url).headers["etag"]
    _rewrite(served.file, _NEW_CLIP)
    reply = served.client.get(served.url, headers={"If-None-Match": tag})
    assert (reply.status_code, reply.content) == (200, _NEW_CLIP)
    assert reply.headers["etag"] != tag


def test_if_range_with_the_current_tag_answers_the_slice(served: Served) -> None:
    tag = served.client.get(served.url).headers["etag"]
    reply = served.client.get(served.url, headers={"Range": "bytes=100-199", "If-Range": tag})
    assert (reply.status_code, reply.content) == (206, _CLIP[100:200])
    assert reply.headers["content-range"] == f"bytes 100-199/{len(_CLIP)}"


def test_if_range_naming_an_older_version_gets_the_whole_current_file(served: Served) -> None:
    old = served.client.get(served.url)
    _rewrite(served.file, _NEW_CLIP)
    reply = served.client.get(
        served.url, headers={"Range": "bytes=100-199", "If-Range": old.headers["etag"]}
    )
    assert (reply.status_code, reply.content) == (200, _NEW_CLIP)
    assert "content-range" not in reply.headers
    assert reply.headers["etag"] != old.headers["etag"]


@pytest.mark.parametrize("validator", ['W/"x"', "Wed, 21 Oct 2015 07:28:00 GMT", "nonsense"])
def test_if_range_only_trusts_the_strong_tag(served: Served, validator: str) -> None:
    modified = served.client.get(served.url).headers["last-modified"]
    for offered in (validator, modified):
        reply = served.client.get(
            served.url, headers={"Range": "bytes=100-199", "If-Range": offered}
        )
        assert (reply.status_code, reply.content) == (200, _CLIP), offered


def test_every_request_leaves_one_receipt(served: Served) -> None:
    forwarded = "company-mini" in str(served.file)
    tag = served.client.get(served.url).headers["etag"]
    with capturing_events() as emitted:
        served.client.get(served.url, headers={"Range": "bytes=0-"})
        served.client.get(served.url, headers={"If-None-Match": tag})
        served.client.get(served.url + "x")
    receipts = _receipts(emitted)
    assert [r["status"] for r in receipts] == [206, 304, 404]
    partial, unchanged, missing = receipts
    assert partial["range"] == "bytes=0-" and partial["conditional"] is False
    assert partial["body_bytes"] == len(_CLIP) == partial["content_length"]
    assert partial["completed"] is True
    assert partial["remote"] is forwarded
    assert (partial["upstream_ms"] is not None) is forwarded
    assert isinstance(partial["ttfb_ms"], float) and isinstance(partial["body_ms"], float)
    assert partial["via_cloudflare"] is False and partial["host"] == "testserver"
    assert unchanged["conditional"] is True and unchanged["body_bytes"] == 0
    assert unchanged["completed"] is True
    assert missing["body_bytes"] == 0 and missing["ttfb_ms"] is None
    assert missing["body_ms"] is None and missing["completed"] is False
    assert missing["remote"] is forwarded


def test_a_receipt_carries_no_path_header_value_or_client_detail(
    served: Served, tmp_path: Path
) -> None:
    with capturing_events() as emitted:
        served.client.get(
            served.url,
            headers={"X-Api-Token": "secret-token", "X-Note": "secret-cookie", "Range": "bytes=0-"},
        )
    rendered = repr(_receipts(emitted))
    for secret in ("secret-token", "secret-cookie", "clip.mp4", str(tmp_path), "testclient"):
        assert secret not in rendered


def test_a_range_header_is_reported_in_one_bounded_shape(served: Served) -> None:
    with capturing_events() as emitted:
        served.client.get(served.url, headers={"Range": "bytes=5-9"})
        served.client.get(served.url, headers={"Range": "bytes=0-1,4-5"})
        served.client.get(served.url, headers={"Range": "items=" + "9" * 5000})
    assert [r["range"] for r in _receipts(emitted)] == ["bytes=5-9", "other", "other"]


def _receipts(emitted: EmittedEvents) -> list[dict[str, object]]:
    return [dict(attributes) for attributes in emitted.attributes("workspace_file_served")]

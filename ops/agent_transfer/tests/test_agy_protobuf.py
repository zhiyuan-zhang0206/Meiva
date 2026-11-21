# meiva:note
# Unit tests for pure Antigravity protobuf and JSON workspace URI rewriting across different-length
# paths, nested messages, varint length boundaries, and malformed payloads.
#
# meiva:link gateway/agents/docs/agent-transfer.md

from __future__ import annotations

import json

import pytest

from ops.agent_transfer.agy_protobuf import (
    path_to_file_uri,
    rewrite_cascade_summary_pb,
    rewrite_trajectory_metadata_pb,
    rewrite_trajectory_pb,
    rewrite_workspace_uris_json,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _len_field(field_num: int, payload: bytes) -> bytes:
    return _varint((field_num << 3) | 2) + _varint(len(payload)) + payload


def _str_field(field_num: int, text: str) -> bytes:
    return _len_field(field_num, text.encode("utf-8"))


def _varint_field(field_num: int, value: int) -> bytes:
    return _varint((field_num << 3) | 0) + _varint(value)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return val, pos
        shift += 7


def _decode_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    out: list[tuple[int, int, bytes | int]] = []
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            val, pos = _read_varint(data, pos)
            out.append((fn, wt, val))
        elif wt == 2:
            size, pos = _read_varint(data, pos)
            out.append((fn, wt, data[pos : pos + size]))
            pos += size
        elif wt in (1, 5):
            width = 8 if wt == 1 else 4
            out.append((fn, wt, data[pos : pos + width]))
            pos += width
        else:
            raise AssertionError(f"unexpected wire type {wt}")
    return out


def _bytes_fields(fields: list[tuple[int, int, bytes | int]], field_num: int) -> list[bytes]:
    return [val for fn, wt, val in fields if fn == field_num and wt == 2 and isinstance(val, bytes)]


def test_rewrite_trajectory_metadata_crosses_128_byte_varint_boundary() -> None:
    src_cwd = "/Users/alice/.meiva/workspaces/7507"
    dst_cwd = "/Users/bob_with_a_very_long_username_crossing_the_128_byte_protobuf_varint_boundary_extra_padding_dir/.meiva/workspaces/7507"
    src_uri = path_to_file_uri(src_cwd)
    dst_uri = path_to_file_uri(dst_cwd)
    assert len(src_uri) < 128 < len(dst_uri)

    ws_msg = _str_field(1, src_uri) + _str_field(2, f"{src_uri}/repo") + _str_field(4, "main")
    created_at = _varint_field(1, 1795286695) + _varint_field(2, 123456)
    blob = (
        _len_field(1, ws_msg)
        + _len_field(2, created_at)
        + _str_field(6, "3880748b-ba0c-49f9-b21c-f8a82ca7bf91")
        + _str_field(7, src_uri)
        + _str_field(18, "default-cli-project")
    )

    rewritten = rewrite_trajectory_metadata_pb(blob, from_cwd=src_cwd, to_cwd=dst_cwd)
    fields = _decode_fields(rewritten)
    (ws_entry,) = _bytes_fields(fields, 1)
    ws_map = {fn: val for fn, _, val in _decode_fields(ws_entry)}

    assert ws_map == {
        1: dst_uri.encode("utf-8"),
        2: f"{dst_uri}/repo".encode(),
        4: b"main",
    }
    assert _bytes_fields(fields, 7) == [dst_uri.encode("utf-8")]
    assert _bytes_fields(fields, 2) == [created_at]
    assert _bytes_fields(fields, 6) == [b"3880748b-ba0c-49f9-b21c-f8a82ca7bf91"]
    assert _bytes_fields(fields, 18) == [b"default-cli-project"]


def test_rewrite_trajectory_metadata_preserves_prefix_colliding_workspace() -> None:
    src_cwd = "/Users/alice/.meiva/workspaces/7507"
    dst_cwd = "/Users/bob_longer/.meiva/workspaces/7507"
    src_uri = path_to_file_uri(src_cwd)
    dst_uri = path_to_file_uri(dst_cwd)
    unrelated_uri = f"{src_uri}0"

    blob = (
        _len_field(1, _str_field(1, src_uri))
        + _len_field(1, _str_field(1, unrelated_uri))
        + _str_field(7, src_uri)
        + _str_field(7, unrelated_uri)
    )

    rewritten = rewrite_trajectory_metadata_pb(blob, from_cwd=src_cwd, to_cwd=dst_cwd)
    fields = _decode_fields(rewritten)
    ws_uris = [_decode_fields(entry)[0][2] for entry in _bytes_fields(fields, 1)]
    assert ws_uris == [dst_uri.encode("utf-8"), unrelated_uri.encode("utf-8")]
    assert _bytes_fields(fields, 7) == [dst_uri.encode("utf-8"), unrelated_uri.encode("utf-8")]


def test_rewrite_trajectory_metadata_longer_to_shorter_roundtrip() -> None:
    src_cwd = "/Users/zhiyuanzhang/.meiva/workspaces/7507"
    dst_cwd = "/Users/zzy/.meiva/workspaces/7507"
    src_uri = path_to_file_uri(src_cwd)
    dst_uri = path_to_file_uri(dst_cwd)

    blob = _len_field(1, _str_field(1, src_uri)) + _str_field(7, src_uri)
    rewritten = rewrite_trajectory_metadata_pb(blob, from_cwd=src_cwd, to_cwd=dst_cwd)
    roundtripped = rewrite_trajectory_metadata_pb(rewritten, from_cwd=dst_cwd, to_cwd=src_cwd)
    assert roundtripped == blob

    fields = _decode_fields(rewritten)
    (ws_bytes,) = _bytes_fields(fields, 1)
    assert _decode_fields(ws_bytes) == [(1, 2, dst_uri.encode("utf-8"))]
    assert _bytes_fields(fields, 7) == [dst_uri.encode("utf-8")]


def test_rewrite_legacy_trajectory_and_cascade_summary_pb() -> None:
    src_cwd = "/Users/zyonzhang/.meiva/workspaces/7507"
    dst_cwd = "/Users/zhiyuanzhang/.meiva/workspaces/7507"
    src_uri = path_to_file_uri(src_cwd)
    dst_uri = path_to_file_uri(dst_cwd)
    assert len(dst_uri) - len(src_uri) == 3

    meta = _len_field(1, _str_field(1, src_uri)) + _str_field(7, src_uri)
    traj = _str_field(2, "3880748b-ba0c-49f9-b21c-f8a82ca7bf91") + _len_field(6, meta)
    rewritten_traj = rewrite_trajectory_pb(traj, from_cwd=src_cwd, to_cwd=dst_cwd)
    traj_fields = {fn: val for fn, _, val in _decode_fields(rewritten_traj)}
    assert traj_fields[2] == b"3880748b-ba0c-49f9-b21c-f8a82ca7bf91"
    meta_bytes = traj_fields[6]
    assert isinstance(meta_bytes, bytes)
    assert dst_uri.encode("utf-8") in meta_bytes
    assert src_uri.encode("utf-8") not in meta_bytes

    summary = (
        _str_field(4, "traj-id")
        + _len_field(9, _str_field(1, src_uri))
        + _len_field(17, meta)
        + _varint_field(22, 4)
    )
    rewritten_summary = rewrite_cascade_summary_pb(summary, from_cwd=src_cwd, to_cwd=dst_cwd)
    summary_fields = {fn: val for fn, _, val in _decode_fields(rewritten_summary)}
    assert summary_fields[4] == b"traj-id"
    assert summary_fields[22] == 4
    ws9 = summary_fields[9]
    tm17 = summary_fields[17]
    assert isinstance(ws9, bytes) and _decode_fields(ws9) == [(1, 2, dst_uri.encode("utf-8"))]
    assert isinstance(tm17, bytes) and dst_uri.encode("utf-8") in tm17
    assert src_uri.encode("utf-8") not in tm17


def test_rewrite_workspace_uris_json_and_malformed_protobuf() -> None:
    src_cwd = "/Users/zyonzhang/.meiva/workspaces/7507"
    dst_cwd = "/Users/zhiyuanzhang/.meiva/workspaces/7507"
    raw = json.dumps(
        [
            "file:///Users/zyonzhang/.meiva/workspaces/7507",
            "file:///Users/zyonzhang/.meiva/workspaces/75070",
        ]
    )
    rewritten = rewrite_workspace_uris_json(raw, from_cwd=src_cwd, to_cwd=dst_cwd)
    assert json.loads(rewritten) == [
        "file:///Users/zhiyuanzhang/.meiva/workspaces/7507",
        "file:///Users/zyonzhang/.meiva/workspaces/75070",
    ]
    assert rewrite_workspace_uris_json("not-json", from_cwd=src_cwd, to_cwd=dst_cwd) == "not-json"

    with pytest.raises(ValueError, match="truncated"):
        _ = rewrite_trajectory_metadata_pb(b"\x0a\x20short", from_cwd=src_cwd, to_cwd=dst_cwd)

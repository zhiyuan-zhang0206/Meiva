# meiva:note
# Pure protobuf wire-format and JSON rewriters for Antigravity (`agy`) persisted workspace URIs
# across cross-machine transfers.
#
# Antigravity persists a conversation's active workspaces and command working directory inside
# length-delimited protobuf blobs (`trajectory_metadata_blob.data` in `conversations/<id>.db`,
# `CortexTrajectory.trajectory_metadata` in legacy `<id>.pb`, and `raw_summary` alongside the
# `workspace_uris` JSON column in `conversation_summaries.db`). Because source and target
# usernames/homes often differ in byte length, rewriting those URIs must decode the wire format
# and recompute every enclosing varint length prefix rather than doing raw byte substitution.
#
# Field numbers are verified against `agy` 1.5.14 protobuf descriptors:
# - `CortexWorkspaceMetadata`: #1 `workspace_folder_absolute_uri` (string), #2 `git_root_absolute_uri` (string)
# - `CortexTrajectoryMetadata`: #1 `workspaces` (repeated CortexWorkspaceMetadata), #7 `workspace_uris` (repeated string)
# - `CortexTrajectory`: #6 `trajectory_metadata` (CortexTrajectoryMetadata)
# - `CascadeTrajectorySummary`: #9 `workspaces` (repeated CortexWorkspaceMetadata), #17 `trajectory_metadata` (CortexTrajectoryMetadata)
#
# meiva:link gateway/agents/docs/agent-transfer.md

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of

type _ProtoFieldRule = Literal["uri"] | Mapping[int, _ProtoFieldRule]
type _ProtoSpec = Mapping[int, _ProtoFieldRule]

_WORKSPACE_METADATA_SPEC: _ProtoSpec = {
    1: "uri",
    2: "uri",
}

_TRAJECTORY_METADATA_SPEC: _ProtoSpec = {
    1: _WORKSPACE_METADATA_SPEC,
    7: "uri",
}

_TRAJECTORY_SPEC: _ProtoSpec = {
    6: _TRAJECTORY_METADATA_SPEC,
}

_CASCADE_SUMMARY_SPEC: _ProtoSpec = {
    9: _WORKSPACE_METADATA_SPEC,
    17: _TRAJECTORY_METADATA_SPEC,
}

_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LEN = 2
_WIRE_FIXED32 = 5
_FIXED_WIDTHS = {_WIRE_FIXED64: 8, _WIRE_FIXED32: 4}


def path_to_file_uri(cwd: str) -> str:
    if not cwd or not cwd.startswith("/"):
        return ""
    normalized = str(PurePosixPath(cwd))
    uri = f"file://{quote(normalized, safe='/')}"
    return uri.rstrip("/") if uri != "file:///" else uri


def _rewrite_file_uri(uri: str, *, old_uri: str, new_uri: str) -> str:
    if not old_uri or not new_uri or old_uri == new_uri:
        return uri
    norm_old = old_uri.rstrip("/") if old_uri != "file:///" else old_uri
    norm_new = new_uri.rstrip("/") if new_uri != "file:///" else new_uri
    norm_uri = uri.rstrip("/") if uri != "file:///" else uri
    if norm_uri == norm_old:
        return norm_new
    prefix = norm_old + "/" if norm_old != "file:///" else norm_old
    if uri.startswith(prefix):
        return norm_new + uri[len(norm_old) :]
    return uri


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    length = len(data)
    while pos < length:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint overflow")
    raise ValueError("protobuf varint truncated")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varint cannot be negative")
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _rewrite_len_field(
    tag: int,
    payload: bytes,
    rule: _ProtoFieldRule,
    old_uri: str,
    new_uri: str,
) -> bytes:
    if rule == "uri":
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            new_payload = payload
        else:
            new_payload = _rewrite_file_uri(text, old_uri=old_uri, new_uri=new_uri).encode("utf-8")
    else:
        new_payload = _rewrite_protobuf_uris(payload, rule, old_uri=old_uri, new_uri=new_uri)
    return _encode_varint(tag) + _encode_varint(len(new_payload)) + new_payload


def _step_field(
    data: bytes,
    pos: int,
    spec: _ProtoSpec,
    old_uri: str,
    new_uri: str,
) -> tuple[bytes, int]:
    tag_start = pos
    tag, pos = _decode_varint(data, pos)
    field_num, wire_type = tag >> 3, tag & 7
    if field_num == 0:
        raise ValueError("invalid protobuf field number 0")
    if wire_type == _WIRE_VARINT:
        _, next_pos = _decode_varint(data, pos)
        return data[tag_start:next_pos], next_pos
    fixed_width = _FIXED_WIDTHS.get(wire_type)
    if fixed_width is not None:
        next_pos = pos + fixed_width
        if next_pos > len(data):
            raise ValueError("protobuf fixed field truncated")
        return data[tag_start:next_pos], next_pos
    if wire_type != _WIRE_LEN:
        raise ValueError(f"unsupported protobuf wire type {wire_type}")
    size, payload_start = _decode_varint(data, pos)
    payload_end = payload_start + size
    if payload_end > len(data):
        raise ValueError("protobuf length-delimited field truncated")
    rule = spec.get(field_num)
    if rule is None:
        return data[tag_start:payload_end], payload_end
    rewritten = _rewrite_len_field(tag, data[payload_start:payload_end], rule, old_uri, new_uri)
    return rewritten, payload_end


def _rewrite_protobuf_uris(
    data: bytes,
    spec: _ProtoSpec,
    *,
    old_uri: str,
    new_uri: str,
) -> bytes:
    if not data or not old_uri or not new_uri or old_uri == new_uri:
        return data
    out = bytearray()
    pos = 0
    total = len(data)
    while pos < total:
        chunk, pos = _step_field(data, pos, spec, old_uri, new_uri)
        out.extend(chunk)
    return bytes(out)


def rewrite_trajectory_metadata_pb(data: bytes, *, from_cwd: str, to_cwd: str) -> bytes:
    return _rewrite_protobuf_uris(
        data,
        _TRAJECTORY_METADATA_SPEC,
        old_uri=path_to_file_uri(from_cwd),
        new_uri=path_to_file_uri(to_cwd),
    )


def rewrite_trajectory_pb(data: bytes, *, from_cwd: str, to_cwd: str) -> bytes:
    return _rewrite_protobuf_uris(
        data,
        _TRAJECTORY_SPEC,
        old_uri=path_to_file_uri(from_cwd),
        new_uri=path_to_file_uri(to_cwd),
    )


def rewrite_cascade_summary_pb(data: bytes, *, from_cwd: str, to_cwd: str) -> bytes:
    return _rewrite_protobuf_uris(
        data,
        _CASCADE_SUMMARY_SPEC,
        old_uri=path_to_file_uri(from_cwd),
        new_uri=path_to_file_uri(to_cwd),
    )


def rewrite_workspace_uris_json(raw_json: str, *, from_cwd: str, to_cwd: str) -> str:
    old_uri = path_to_file_uri(from_cwd)
    new_uri = path_to_file_uri(to_cwd)
    if not raw_json or not old_uri or not new_uri or old_uri == new_uri:
        return raw_json
    try:
        parsed = object_of(json.loads, raw_json)
    except ValueError:
        return raw_json
    if not is_object_list(parsed):
        return raw_json
    rewritten: list[object] = [
        _rewrite_file_uri(item, old_uri=old_uri, new_uri=new_uri) if isinstance(item, str) else item
        for item in parsed
    ]
    if rewritten == parsed:
        return raw_json
    return json.dumps(rewritten, separators=(",", ":"))

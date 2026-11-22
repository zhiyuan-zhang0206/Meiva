# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from mcp.types import CallToolResult, TextContent

from base.host.boundary.json_values import is_object_dict, is_object_list

from .workspace_files import read_workspace_file, workspace_path_for, write_workspace_file

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RPC_BYTES = 4 * ((MAX_FILE_BYTES + 2) // 3) + 64 * 1024
_FILE_TOOLS = frozenset({"upload_file", "take_screenshot", "take_snapshot"})
_PATH_FIELDS = frozenset(
    {
        "filePath",
        "outputDirPath",
        "requestFilePath",
        "responseFilePath",
        "baseFilePath",
        "currentFilePath",
        "path",
    }
)
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


class Artifact(TypedDict):
    path: str
    data: str


def file_argument(tool: str, args: Mapping[str, object]) -> bool:
    allowed: set[str] = {"filePath"} if tool in _FILE_TOOLS else set()
    unknown = _PATH_FIELDS.intersection(args) - allowed
    if unknown:
        raise ValueError(
            f"Remote Chrome does not support these file arguments for {tool}: {sorted(unknown)}"
        )
    return "filePath" in args


def encode_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_bytes(data: object, *, limit: int = MAX_FILE_BYTES) -> bytes:
    if not isinstance(data, str) or len(data) > 4 * ((limit + 2) // 3):
        raise ValueError("Chrome file transfer has an invalid or oversized payload")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Chrome file transfer has invalid base64") from exc
    if len(raw) > limit:
        raise ValueError("Chrome file transfer exceeds its byte limit")
    return raw


def upload_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= 180
        or _INVALID_NAME.search(value)
        or value.endswith((".", " "))
    ):
        raise ValueError("Remote Chrome upload needs a portable filename")
    if value.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        raise ValueError("Remote Chrome upload filename is reserved on Windows")
    return value


class CallerFiles:
    def __init__(self, workspace: Path, tool: str, args: Mapping[str, object]) -> None:
        self.workspace = workspace
        self.args = dict(args)
        self.inputs: dict[str, str] = {}
        self.destination: Path | None = None
        if file_argument(tool, args):
            path = workspace_path_for(self.workspace, args["filePath"])
            if tool == "upload_file":
                self.args["filePath"] = upload_name(path.name)
                self.inputs["filePath"] = encode_bytes(
                    read_workspace_file(self.workspace, path, limit=MAX_FILE_BYTES)
                )
            else:
                self.args["filePath"] = ""
                self.destination = path

    def receive(self, result: CallToolResult, raw: object) -> CallToolResult:
        if not is_object_dict(raw):
            raise ValueError("Chrome peer returned invalid file metadata")
        if result.is_error:
            return result
        if not raw:
            if self.destination is not None:
                raise ValueError("Remote Chrome did not return the requested output file")
            return result
        if self.destination is None or set(raw) != {"filePath"}:
            raise ValueError("Chrome peer returned unexpected output files")
        entries = raw["filePath"]
        if (
            not is_object_list(entries)
            or len(entries) != 1
            or not is_object_dict(entry := entries[0])
        ):
            raise ValueError("Chrome peer returned invalid output files")
        source = entry.get("path")
        if not isinstance(source, str) or Path(source).name != source:
            raise ValueError("Chrome peer returned an unexpected output name")
        suffix = Path(source).suffix
        if suffix not in {".png", ".jpeg", ".jpg", ".webp", ".txt"}:
            raise ValueError("Chrome peer returned an unexpected output format")
        dest = self.destination.with_suffix(suffix)
        write_workspace_file(self.workspace, dest, decode_bytes(entry.get("data")))
        return replace_paths(result, {source: str(dest)})


def replace_paths(result: CallToolResult, replacements: Mapping[str, str]) -> CallToolResult:
    content = list(result.content)
    for index, item in enumerate(content):
        if isinstance(item, TextContent):
            text = item.text
            for source, destination in replacements.items():
                text = text.replace(source, destination)
            content[index] = item.model_copy(update={"text": text})
    return result.model_copy(update={"content": content})


@contextmanager
def stage_upload(
    args: Mapping[str, object],
    inputs: Mapping[str, object],
    *,
    parent: Path,
) -> Generator[dict[str, object], None, None]:
    if set(inputs) != {"filePath"} or "filePath" not in args:
        raise ValueError("Chrome upload is missing its staged file data")
    name = upload_name(args["filePath"])
    data = decode_bytes(inputs["filePath"])
    with TemporaryDirectory(prefix="chrome-", dir=parent) as scratch:
        path = Path(scratch) / name
        _ = path.write_bytes(data)
        yield {**args, "filePath": str(path)}

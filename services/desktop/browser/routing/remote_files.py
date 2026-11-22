# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import base64
import binascii
import re
import stat
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from mcp.types import CallToolResult, TextContent

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.private_storage import write_private_bytes

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RPC_BYTES = 4 * ((MAX_FILE_BYTES + 2) // 3) + 64 * 1024
_UPLOAD_FIELD = "filePath"
_OUTPUT_FIELDS = {
    "take_screenshot": ("filePath",),
    "take_snapshot": ("filePath",),
    "evaluate_script": ("filePath",),
    "get_network_request": ("requestFilePath", "responseFilePath"),
    "performance_start_trace": ("filePath",),
    "performance_stop_trace": ("filePath",),
    "take_heapsnapshot": ("filePath",),
    "lighthouse_audit": ("outputDirPath",),
}
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


def fields_for(tool: str, args: Mapping[str, object]) -> tuple[str, ...]:
    fields = (_UPLOAD_FIELD,) if tool == "upload_file" else _OUTPUT_FIELDS.get(tool, ())
    unknown = _PATH_FIELDS.intersection(args) - set(fields)
    if unknown:
        raise ValueError(
            f"Remote Chrome does not support these file arguments for {tool}: {sorted(unknown)}"
        )
    return tuple(field for field in fields if field in args)


def bounded_read(path: Path, *, limit: int = MAX_FILE_BYTES) -> bytes:
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Chrome transfer requires a regular file")
    with path.open("rb") as stream:
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise ValueError(f"Chrome file transfer exceeds the {limit} byte limit")
    return result


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


def workspace_file(workspace: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Chrome file path must name a file inside the agent workspace")
    path = Path(value)
    if ".." in path.parts or not (path := (workspace / path).resolve()).is_relative_to(workspace):
        raise ValueError("Chrome file path escapes the agent workspace")
    if path == workspace:
        raise ValueError("Chrome file path must name an entry inside the agent workspace")
    return path


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
        self.workspace = workspace.resolve()
        self.args = dict(args)
        self.inputs: dict[str, str] = {}
        self.destinations: dict[str, Path] = {}
        for field in fields_for(tool, args):
            path = workspace_file(self.workspace, args[field])
            if tool == "upload_file":
                self.args[field] = upload_name(path.name)
                self.inputs[field] = encode_bytes(bounded_read(path))
            else:
                self.args[field] = path.suffix if field != "outputDirPath" else ""
                self.destinations[field] = path

    def receive(self, result: CallToolResult, raw: object) -> CallToolResult:
        if not is_object_dict(raw) or set(raw) - self.destinations.keys():
            raise ValueError("Chrome peer returned unexpected output files")
        if result.is_error:
            return result
        remaining = MAX_FILE_BYTES
        replacements: dict[str, str] = {}
        files: list[tuple[Path, bytes]] = []
        for field, entries in raw.items():
            if not is_object_list(entries) or len(entries) > (2 if field == "outputDirPath" else 1):
                raise ValueError("Chrome peer returned invalid output files")
            for entry in entries:
                if not is_object_dict(entry) or not isinstance(source := entry.get("path"), str):
                    raise ValueError("Chrome peer returned invalid output file metadata")
                dest = self._artifact_destination(field, source)
                data = decode_bytes(entry.get("data"), limit=remaining)
                remaining -= len(data)
                files.append((dest, data))
                replacements[source] = str(dest)
                if field == "outputDirPath":
                    replacements[str(Path(source).parent)] = str(dest.parent)
        for dest, data in files:
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_private_bytes(dest, data)
        return replace_paths(result, replacements)

    def _artifact_destination(self, field: str, source: str) -> Path:
        dest = self.destinations[field]
        filename = Path(source).name
        if field == "outputDirPath":
            if filename not in {"report.html", "report.json"}:
                raise ValueError("Chrome peer returned an unexpected Lighthouse report")
            dest = dest / filename
        elif suffix := Path(filename).suffix:
            dest = dest.with_suffix(suffix)
        return workspace_file(self.workspace, str(dest))


def replace_paths(result: CallToolResult, replacements: Mapping[str, str]) -> CallToolResult:
    content = list(result.content)
    for index, item in enumerate(content):
        if isinstance(item, TextContent):
            text = item.text
            for source, destination in replacements.items():
                text = text.replace(source, destination)
            content[index] = item.model_copy(update={"text": text})
    return result.model_copy(update={"content": content})


class StagedFiles:
    def __init__(self, args: dict[str, object], destinations: dict[str, Path]) -> None:
        self.args = args
        self.destinations = destinations

    def outputs(self) -> dict[str, list[Artifact]]:
        result: dict[str, list[Artifact]] = {}
        remaining = MAX_FILE_BYTES
        for field, path in self.destinations.items():
            directory = path if field == "outputDirPath" else path.parent
            entries: list[Artifact] = []
            for file in sorted(directory.iterdir()):
                if (
                    file.is_symlink()
                    or not file.is_file()
                    or len(entries) == (2 if field == "outputDirPath" else 1)
                ):
                    raise ValueError("Chrome produced an unexpected staged output")
                data = bounded_read(file, limit=remaining)
                remaining -= len(data)
                entries.append({"path": str(file), "data": encode_bytes(data)})
            result[field] = entries
        return result


@contextmanager
def stage_files(
    tool: str,
    args: Mapping[str, object],
    inputs: Mapping[str, object],
    *,
    parent: Path,
) -> Generator[StagedFiles, None, None]:
    fields = fields_for(tool, args)
    if set(inputs) != (set(fields) if tool == "upload_file" else set()):
        raise ValueError("Chrome upload is missing its staged file data")
    if not fields:
        yield StagedFiles(dict(args), {})
        return
    with TemporaryDirectory(prefix="chrome-", dir=parent) as scratch:
        staged_args = dict(args)
        outputs: dict[str, Path] = {}
        for field in fields:
            directory = Path(scratch) / field
            directory.mkdir(mode=0o700)
            if tool == "upload_file":
                path = directory / upload_name(args[field])
                path.write_bytes(decode_bytes(inputs[field]))
            elif field == "outputDirPath":
                path = directory
                outputs[field] = path
            else:
                suffix = args[field]
                if not isinstance(suffix, str) or not re.fullmatch(
                    r"(?:\.[a-zA-Z0-9]{1,16})?", suffix
                ):
                    raise ValueError("Invalid file extension for remote Chrome output")
                path = directory / f"output{suffix}"
                outputs[field] = path
            staged_args[field] = str(path)
        yield StagedFiles(staged_args, outputs)

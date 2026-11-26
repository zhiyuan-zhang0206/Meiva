from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.env.harness_binaries import resolve_user_home
from base.lm.harness.antigravity.transcript.text import legacy_marker_digest, request_body
from base.lm.harness.events import InputLost, InputRejected
from base.lm.harness.session.transcript.record_file import FileRow, RecordFile

ROW_KEY = "meiva_input_key"
LEDGER_NAME = "meiva-inputs.jsonl"
RECORD_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
MAX_SCAN_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SentInput:
    key: str
    min_offset: int
    text_sha256: str
    image_count: int


@dataclass(frozen=True, slots=True)
class Ledger:
    sent: tuple[SentInput, ...] = ()
    bound: Mapping[int, str] = field(default_factory=dict[int, str])
    dropped: tuple[str, ...] = ()


def inputs_path(record: Path) -> Path:
    return record.with_name(LEDGER_NAME)


def inputs_path_for(home: Path | None, conversation_id: str) -> Path:
    return (
        resolve_user_home(home)
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / LEDGER_NAME
    )


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_text_sha256(blocks: Sequence[Mapping[str, str]]) -> str:
    return text_sha256(
        "".join(block["text"] for block in blocks if block["type"] == "text").strip()
    )


def prompt_image_count(blocks: Sequence[Mapping[str, str]]) -> int:
    return sum(1 for block in blocks if block["type"] == "image")


def load_ledger(path: Path) -> Ledger:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return Ledger()
    lines = text.split("\n")
    unterminated = len(lines) if text and not text.endswith("\n") else None
    sent: list[SentInput] = []
    bound: dict[int, str] = {}
    dropped: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = object_of(json.loads, line)
        except json.JSONDecodeError:
            if number == unterminated:
                continue
            raise
        _read_line(path, number, row, sent, bound, dropped)
    return Ledger(tuple(sent), bound, tuple(dropped))


def _read_line(
    path: Path,
    number: int,
    row: object,
    sent: list[SentInput],
    bound: dict[int, str],
    dropped: list[str],
) -> None:
    if is_object_dict(row):
        kind, key = row.get("kind"), row.get("key")
        digest = row.get("text_sha256")
        floor, images, offset = (
            _count(row.get(name)) for name in ("min_offset", "image_count", "offset")
        )
        if (
            kind == "sent"
            and isinstance(key, str)
            and isinstance(digest, str)
            and floor is not None
            and images is not None
        ):
            sent.append(SentInput(key, floor, digest, images))
            return
        if kind == "bound" and isinstance(key, str) and offset is not None:
            bound[offset] = key
            return
        if kind == "dropped" and isinstance(key, str):
            dropped.append(key)
            return
    raise ValueError(f"{path}:{number}: an input ledger line of an unexpected shape")


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _append(path: Path, build: Callable[[Ledger], Sequence[Mapping[str, object]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = build(load_ledger(path))
        if not lines:
            return
        data = "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)
        with path.open("ab") as handle:
            handle.write(data.encode())
            handle.flush()
            os.fsync(handle.fileno())


def record_sent_input(
    path: Path, *, key: str, min_offset: int, text_sha256: str, image_count: int
) -> None:
    line = {
        "kind": "sent",
        "key": key,
        "min_offset": min_offset,
        "text_sha256": text_sha256,
        "image_count": image_count,
    }
    _append(path, lambda _ledger: [line])


def record_bound_inputs(path: Path, bindings: Mapping[int, str]) -> None:
    _append(
        path,
        lambda ledger: [
            {"kind": "bound", "offset": offset, "key": key}
            for offset, key in bindings.items()
            if ledger.bound.get(offset) != key
        ],
    )


def drop_input(home: Path | None, conversation_id: str | None, event: object) -> None:
    if conversation_id is None or not isinstance(event, (InputLost, InputRejected)):
        return
    _append(
        inputs_path_for(home, conversation_id),
        lambda ledger: (
            [{"kind": "dropped", "key": event.key}]
            if any(sent.key == event.key for sent in unbound_inputs(ledger))
            else []
        ),
    )


def unbound_inputs(ledger: Ledger) -> list[SentInput]:
    unconsumed = Counter(ledger.bound.values()) + Counter(ledger.dropped)
    unbound: list[SentInput] = []
    for sent in ledger.sent:
        if unconsumed[sent.key] > 0:
            unconsumed[sent.key] -= 1
        else:
            unbound.append(sent)
    return unbound


def resolve_inputs(
    rows: Iterable[tuple[int, Mapping[str, object]]],
    ledger: Ledger,
    scan_bytes: int = MAX_SCAN_BYTES,
) -> dict[int, str]:
    found = list(rows)
    floor = max((offset for offset, _row in found), default=0) - scan_bytes
    unbound = [sent for sent in unbound_inputs(ledger) if sent.min_offset >= floor]
    return resolve_unbound(found, ledger.bound, unbound)


def resolve_unbound(
    rows: Sequence[tuple[int, Mapping[str, object]]],
    bound: Mapping[int, str],
    unbound: Sequence[SentInput],
) -> dict[int, str]:
    resolved = dict(bound)
    for sent in unbound:
        offset = next(
            (
                offset
                for offset, row in rows
                if offset >= sent.min_offset and offset not in resolved and answers(row, sent)
            ),
            None,
        )
        if offset is not None:
            resolved[offset] = sent.key
    return resolved


def answers(row: Mapping[str, object], sent: SentInput) -> bool:
    content = row.get("content")
    if row.get("type") != "USER_INPUT" or not isinstance(content, str):
        return False
    body = request_body(content)
    if body is None or legacy_marker_digest(content) is not None:
        return False
    if text_sha256(body) != sent.text_sha256:
        return False
    media = row.get("media")
    return bool(body) or (len(media) if is_object_list(media) else 0) == sent.image_count


def bind_inputs(
    record: Path, records: Sequence[FileRow]
) -> tuple[list[dict[str, object]], tuple[Path, dict[int, str]] | None]:
    path = inputs_path(record)
    ledger = load_ledger(path)
    resolved = resolve_inputs(((record.offset, record.row) for record in records), ledger)
    rows = [
        {**record.row, ROW_KEY: resolved[record.offset]}
        if record.offset in resolved
        else dict(record.row)
        for record in records
    ]
    fresh = {o: k for o, k in resolved.items() if o not in ledger.bound}
    return rows, (path, fresh) if fresh else None


def row_input_key(row: Mapping[str, object], keys: Sequence[str]) -> str | None:
    bound, content = row.get(ROW_KEY), row.get("content")
    if isinstance(bound, str):
        return bound if bound in keys else None
    digest = legacy_marker_digest(content) if isinstance(content, str) else None
    if row.get("type") != "USER_INPUT" or digest is None:
        return None
    return next((key for key in keys if text_sha256(key) == digest), None)


def received_keys(rows: Iterable[Mapping[str, object]], keys: Sequence[str]) -> set[str]:
    return {key for row in rows if (key := row_input_key(row, keys)) is not None}


def settle_inputs(
    rows: Iterable[Mapping[str, object]],
    fresh: tuple[Path, Mapping[int, str]] | None,
    keys: Sequence[str],
) -> set[str]:
    if fresh:
        record_bound_inputs(*fresh)
    return received_keys(rows, keys)


def log_prompt(
    home: Path | None, conversation_id: str | None, key: str, blocks: Sequence[Mapping[str, str]]
) -> None:
    if conversation_id is None:
        raise RuntimeError("an input is sent before the conversation has an id")
    ledger = inputs_path_for(home, conversation_id)
    record = next(
        (ledger.with_name(name) for name in RECORD_NAMES if ledger.with_name(name).is_file()), None
    )
    record_sent_input(
        ledger,
        key=key,
        min_offset=0 if record is None else RecordFile(record).complete_end(),
        text_sha256=prompt_text_sha256(blocks),
        image_count=prompt_image_count(blocks),
    )

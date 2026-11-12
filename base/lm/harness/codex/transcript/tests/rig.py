from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path

from base.lm.harness.codex.transcript.link import TranscriptLink
from base.lm.harness.codex.transcript.reader import CodexRecordReader
from base.lm.harness.codex.transcript.shapes import Obj
from base.lm.harness.codex.transcript.tests.history_fake import FakeHistory, write_lines
from base.lm.harness.codex.transcript.tests.support import as_obj, load_json, load_lines, page_rows
from base.lm.harness.session.transcript.entries import MAIN_RECORD, AnyEntry
from base.lm.harness.session.transcript.ports import EntryBatch

# meiva:note
# A Codex thread to read in a test: the fake item history, the rollout file next to it (every item the app-server
# serves is a line the rollout gained, which is the growth the reader's quiet check looks at), a link, a clock the test
# moves and the real reader over them. `drain` reads a record until the reader has nothing, keeping the position the
# way the writer does.
# meiva:link base/lm/harness/docs/transcript-projection.md

THREAD = "thr-main"
STAMP = "2026-11-12T08:57:13.042Z"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Threads:
    def __init__(self) -> None:
        self.by_id: dict[str, FakeHistory] = {}

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float | None = None
    ) -> dict[str, object]:
        return await self.by_id[str(params["threadId"])].request(method, params, timeout=timeout)


class Rig:
    def __init__(self, tmp_path: Path, thread: str = THREAD) -> None:
        self.history = FakeHistory(thread)
        self.threads = Threads()
        self.threads.by_id[thread] = self.history
        self.home = tmp_path
        self.folder = tmp_path / "sessions" / "2026" / "11" / "12"
        self.folder.mkdir(parents=True)
        self.rollout = self.folder / f"rollout-2026-11-12T10-00-00-{thread}.jsonl"
        self.rollout.touch()
        self.rollouts: dict[str, Path] = {thread: self.rollout}
        self.woken: list[str | None] = []
        self.link = TranscriptLink(self.woken.append)
        self.link.attach(self.threads, rollout_path=str(self.rollout), cli_version="0.166.0")
        self.clock = Clock()
        self.reader = CodexRecordReader(
            codex_home=tmp_path, thread_id=lambda: thread, link=self.link, now=self.clock
        )
        self.position: dict[str, str | None] = {}
        self.lines = 1000

    def restarted_reader(self) -> CodexRecordReader:
        return CodexRecordReader(
            codex_home=self.home,
            thread_id=lambda: self.history.thread,
            link=self.link,
            now=self.clock,
        )

    def add_child(self, thread: str) -> None:
        self.threads.by_id[thread] = FakeHistory(thread)
        path = self.folder / f"rollout-2026-11-12T10-00-01-{thread}.jsonl"
        path.touch()
        self.rollouts[thread] = path

    def feed_child(self, thread: str, rows: Sequence[Obj], lines: Sequence[Obj] = ()) -> None:
        for row in rows:
            self.threads.by_id[thread].append(row)
        write_lines(self.rollouts[thread], list(lines))
        self.link.on_notification("item/completed", {"threadId": thread, "turnId": "t"})

    def feed(self, rows: Sequence[Obj], lines: Sequence[Obj] = ()) -> None:
        for row in rows:
            self.history.append(row)
        filler: Obj = {
            "type": "turn_context",
            "timestamp": STAMP,
            "ordinal": self.lines,
            "payload": {},
        }
        self.lines += 1
        write_lines(self.rollout, [*lines, filler])

    async def drain(self, record: str = MAIN_RECORD) -> list[EntryBatch]:
        batches: list[EntryBatch] = []
        while (
            batch := await self.reader.read_after(record, self.position.get(record))
        ) is not None:
            self.position[record] = batch.position
            batches.append(batch)
            assert len(batches) < 1000
        return batches


def entries_of(batches: list[EntryBatch]) -> list[AnyEntry]:
    return [entry for batch in batches for entry in batch.entries]


def basic() -> tuple[list[Obj], list[Obj]]:
    rows = [as_obj(row) for row in page_rows(copy.deepcopy(load_json("basic/items.json")))]
    return rows, load_lines("basic/rollout.jsonl")

"""Process-commit — the commit a *running process* actually loaded.

The other commit signals are **disk** state: ``head_sha`` is what the
checkout is at right now, ``running_sha`` what ``meiva start`` last started on
(`base.deploy.git.running_sha`). Neither answers "what is this daemon executing?" — a
bookmark can be rewritten while an already-live process keeps its old code, so
a daemon can sit on code from days ago while every bookmark reads as current.
That is not hypothetical: on 2026-07-26 a unit's ops daemon served a
capability set it had cached at boot for two days after the file it derives
from changed, and three separate signals agreed it was aligned.

Each executable entry captures one immutable `LoadedCommit` when it loads its
code and passes that fact to its consumers. Reading `sha` never reads Git or
substitutes the current checkout. Unknown stays unknown even if the checkout
later becomes available; a new process entry owns a new capture.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from base.native_process.untyped import is_object_dict, object_of

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT_S = 10

# meiva:note
# A sealed release tree carries no `.git`; this file at its root is where its identity lives
# instead. The stager writes it before sealing, so it is part of the sealed tree.
# meiva:link future/infra/lifecycle/release-dir-atomic-code-swap.md
RELEASE_RECORD = ".meiva-release.json"
RELEASE_SCHEMA = "meiva-source-release/v1"
_RECORD_FIELDS = frozenset({"schema", "commit", "code_version", "committed_at"})
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


class ReleaseRecordError(ValueError):
    pass


# meiva:note
# The identity of one release tree: the commit it was archived from, that commit's first-parent
# count (computed where the full history exists, so a tree without history still orders against
# the code-version gate) and its commit time in UTC. Construction validates every field, so a
# record that exists is one a reader can trust as far as its shape goes.
@dataclass(frozen=True)
class ReleaseRecord:
    commit: str
    code_version: int
    committed_at: str

    def __post_init__(self) -> None:
        if not _FULL_SHA.fullmatch(self.commit):
            raise ReleaseRecordError(f"commit must be 40 lowercase hex digits: {self.commit!r}")
        if type(self.code_version) is not int or self.code_version < 1:
            raise ReleaseRecordError(
                f"code_version must be a positive integer: {self.code_version!r}"
            )
        try:
            moment = datetime.fromisoformat(self.committed_at)
        except ValueError as exc:
            raise ReleaseRecordError(
                f"committed_at is not ISO-8601: {self.committed_at!r}"
            ) from exc
        if moment.utcoffset() != timedelta(0):
            raise ReleaseRecordError(f"committed_at must be UTC: {self.committed_at!r}")

    def to_json(self) -> str:
        body = {
            "schema": RELEASE_SCHEMA,
            "commit": self.commit,
            "code_version": self.code_version,
            "committed_at": self.committed_at,
        }
        return json.dumps(body, indent=2, sort_keys=True) + "\n"

    @classmethod
    def parse(cls, raw: str, *, origin: str) -> ReleaseRecord:
        try:
            data = object_of(json.loads, raw)
        except json.JSONDecodeError as exc:
            raise ReleaseRecordError(f"{origin}: not JSON: {exc}") from exc
        if not is_object_dict(data):
            raise ReleaseRecordError(f"{origin}: a release record is a JSON object")
        if frozenset(data) != _RECORD_FIELDS:
            raise ReleaseRecordError(
                f"{origin}: fields must be exactly {sorted(_RECORD_FIELDS)}, got {sorted(data)}"
            )
        if data["schema"] != RELEASE_SCHEMA:
            raise ReleaseRecordError(f"{origin}: schema {data['schema']!r} is not {RELEASE_SCHEMA}")
        commit, version, committed = data["commit"], data["code_version"], data["committed_at"]
        if not isinstance(commit, str) or not isinstance(committed, str):
            raise ReleaseRecordError(f"{origin}: commit and committed_at must be strings")
        if type(version) is not int:
            raise ReleaseRecordError(f"{origin}: code_version must be an integer")
        try:
            return cls(commit=commit, code_version=version, committed_at=committed)
        except ReleaseRecordError as exc:
            raise ReleaseRecordError(f"{origin}: {exc}") from exc


# meiva:note
# None when the tree is not a release (no record at its root); a record that exists but does not
# parse raises, because a release whose identity cannot be read is not a checkout either.
def read_release_record(source_root: Path) -> ReleaseRecord | None:
    path = source_root / RELEASE_RECORD
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return ReleaseRecord.parse(raw, origin=str(path))


def capture_commit(
    source_root: Path,
    *,
    git_executable: str = "git",
    timeout_s: float = _GIT_TIMEOUT_S,
) -> str | None:
    """Read the source commit once at an explicit process entry point."""
    # meiva:note
    # git_executable defaults to the trusted "git" binary or an explicit test probe path. A release
    # tree answers from its record and never runs Git; a record that cannot be read is an unknown
    # commit, like a failed `rev-parse`.
    try:
        record = read_release_record(source_root)
    except (OSError, ReleaseRecordError):
        return None
    if record is not None:
        return record.commit
    try:
        result = subprocess.run(  # noqa: S603
            [git_executable, "rev-parse", "HEAD"],
            cwd=source_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# meiva:note
# The first-parent count a release record carries, read at the same entry as `capture_commit`.
# None for a Git checkout, whose count is asked of Git when first needed, and for a record that
# cannot be read, whose commit is unknown as well.
def capture_code_version(source_root: Path) -> int | None:
    try:
        record = read_release_record(source_root)
    except (OSError, ReleaseRecordError):
        return None
    return None if record is None else record.code_version


@dataclass(frozen=True)
class LoadedCommit:
    """One process entry's captured source image, including an honest unknown.

    Consumers read ``sha`` rather than Git. A missing capture stays missing even
    when the checkout later becomes available or moves underneath the process.
    """

    source_root: Path
    sha: str | None
    # meiva:note
    # The first-parent count a release record carried, captured with the commit; None for a Git
    # checkout, whose count `code_version.CodeVersion` asks Git for.
    code_version: int | None = None

    # meiva:note
    # Capturing is bookkeeping and must not take a process down: an unknown commit is refused
    # loudly by the code-version gate at its first dial.
    @classmethod
    def capture(
        cls,
        source_root: Path = _SOURCE_ROOT,
        *,
        git_executable: str = "git",
        timeout_s: float = _GIT_TIMEOUT_S,
    ) -> LoadedCommit:
        return cls(
            source_root=source_root,
            sha=capture_commit(source_root, git_executable=git_executable, timeout_s=timeout_s),
            code_version=capture_code_version(source_root),
        )

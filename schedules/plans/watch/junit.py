# meiva:note
# What a run's JUnit artifact says that its log did not: the cases that failed when the log named
# none, and which artifact belongs to which job. It lives apart from `main_red`, whose decisions are
# about cards and entries: this file only turns names into names (task #6487).
#
# The id is built the way the log builds it -- `file::name` -- so one test keeps one fingerprint
# whichever route named it. A dotted `classname` would make the same case look like two, and the
# second sighting that turns a suspect into a card would never arrive.
# meiva:link schedules/README.md

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from xml.etree import ElementTree

__all__ = [
    "ArtifactMatch",
    "artifact_candidates",
    "artifact_name_for_job",
    "failing_case_ids",
    "match_artifact",
    "matching_artifact",
    "shard_number",
]


# meiva:note
# The artifact a job's JUnit lands in, as a name this module derives from the job instead of a table
# someone has to keep current. Every shard job names its artifact `test-results-<job>`, with a `2/4`
# shard written as `2` -- so `e2e shard (2/4)` is `test-results-e2e-shard-2` and
# `backend static 4/5` is `test-results-backend-static-4`. A name this does not reproduce is not
# guessed at: the caller matches by prefix and falls back to the unnamed failure.
def artifact_name_for_job(job_name: str) -> str:
    trimmed = re.sub(r"(\d+)/\d+", r"\1", job_name)
    return "test-results-" + re.sub(r"[^a-z0-9]+", "-", trimmed.lower()).strip("-")


# meiva:note
# The artifact whose name belongs to this job, out of the ones a run published. Three answers, in
# order, because a run can publish several names that overlap: the exact name; else a name that
# extends it (the same job with a suffix, so the longer one is the more specific); else a name the
# wanted one extends -- a job whose name is more specific than its artifact, which happens when the
# job carries a matrix label the artifact does not. Within a tier the longest wins. No candidate
# answers nothing rather than the nearest name: naming a case from another shard would put a real
# test id on the wrong red, which is worse than saying the watch could not name it.
def matching_artifact(job_name: str, names: Sequence[str]) -> str | None:
    return match_artifact(job_name, names).name


# meiva:note
# Every artifact this module will consider is a JUnit bundle; `e2e-logs-matrix-3` is a log bundle
# that would otherwise win a suffix match on the same shard number.
_ARTIFACT_PREFIX = "test-results-"


# meiva:note
# The shard a job name declares, when it declares one: `e2e full matrix (3/4)` answers `3` and
# `backend shard 6/8` answers `6`. Written because a matrix job names its upload after the shard
# (`test-results-e2e-shard-3`) while the job itself is named after the lane and the population
# (`e2e full matrix (3/4)`), so the derived name cannot reach it (2026-11-08, three cards unnamed).
def shard_number(job_name: str) -> str | None:
    match = re.search(r"(\d+)\s*/\s*\d+", job_name)
    return match.group(1) if match else None


# meiva:note
# The artifacts a reader should look at when none of them belongs to this job: names in this run's
# bundle family that share a word with the job. The point is not to guess -- a name picked from here
# never names a case -- but to answer "where do I look" on the card instead of leaving the reader to
# open the run and diff the artifact list by eye.
def _shares_a_word(job_name: str, artifact: str) -> bool:
    words = {word for word in re.split(r"[^a-z0-9]+", job_name.lower()) if len(word) > 2}
    return bool(
        words & {word for word in re.split(r"[^a-z0-9]+", artifact.lower()) if len(word) > 2}
    )


def artifact_candidates(job_name: str, names: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            name
            for name in names
            if name.startswith(_ARTIFACT_PREFIX) and _shares_a_word(job_name, name)
        )
    )


# meiva:note
# Which artifact this job's JUnit is in, and how the answer was reached. Three tiers, and the third
# is the important one: the derived name (a job that names its own upload); else, for a matrix job,
# the one artifact whose name ends with the shard the job declares; else nothing, with the near
# misses listed so the card can say where to look. A wrong shard would put a real test id on another
# job's red, which is worse than saying the watch could not name it.
@dataclass(frozen=True)
class ArtifactMatch:
    name: str | None
    how: str
    candidates: tuple[str, ...]
    # meiva:note
    # What the card says when the name came from the shard tier rather than from the job's own
    # upload name: exact and heuristic matches have to be distinguishable to a reader, or the next
    # person asking "is this case really this card's" has no way to tell whether to trust it (405,
    # 2026-11-08). Empty for an exact match and for no match -- the other two branches already say
    # what they are.
    note: str = ""


def match_artifact(job_name: str, names: Sequence[str]) -> ArtifactMatch:
    wanted = artifact_name_for_job(job_name)
    if wanted in names:
        return ArtifactMatch(wanted, "name", ())
    longer = [name for name in names if name.startswith(wanted)]
    if longer:
        return ArtifactMatch(max(longer, key=len), "name", ())
    shorter = [name for name in names if wanted.startswith(name)]
    if shorter:
        return ArtifactMatch(max(shorter, key=len), "name", ())
    shard = shard_number(job_name)
    if shard is not None:
        by_shard = [
            name
            for name in names
            if name.startswith(_ARTIFACT_PREFIX)
            and name.endswith(f"-{shard}")
            and _shares_a_word(job_name, name)
        ]
        if len(by_shard) == 1:
            return ArtifactMatch(
                by_shard[0],
                "shard",
                (),
                f"{by_shard[0]} (matched by shard: the job name gives no upload name)",
            )
    return ArtifactMatch(None, "none", artifact_candidates(job_name, names))


# meiva:note
# The cases a JUnit document reports as failed or errored, as `file::name` where the document names
# a file. `error` counts with `failure` -- a case that blew up is a red nobody retires by passing --
# and a skipped case is not a red at all. A document that cannot be parsed answers nothing rather
# than raising: an unreadable artifact is the same evidence as a missing one, and the caller has a
# name for that already.
def failing_case_ids(xml_text: str) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return ()
    found: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        name = case.get("name") or ""
        location = case.get("file") or case.get("classname") or ""
        if name and location:
            found.append(f"{location}::{name}")
    return tuple(dict.fromkeys(found))

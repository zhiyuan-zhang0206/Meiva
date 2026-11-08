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
from xml.etree import ElementTree

__all__ = ["artifact_name_for_job", "failing_case_ids", "matching_artifact"]


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
    wanted = artifact_name_for_job(job_name)
    if wanted in names:
        return wanted
    longer = [name for name in names if name.startswith(wanted)]
    if longer:
        return max(longer, key=len)
    shorter = [name for name in names if wanted.startswith(name)]
    if shorter:
        return max(shorter, key=len)
    return None


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

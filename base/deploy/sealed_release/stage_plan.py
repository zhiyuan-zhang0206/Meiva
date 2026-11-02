# meiva:note
# What staging and switching a release decide, as values: where a home keeps its releases and its
# `source` pointer, a release's name, whether a stage reuses, rebuilds or builds, and whether a
# switch may happen. `stage.py` runs these; the build commands are decided in `build_plan.py`.
# meiva:link future/infra/lifecycle/release-dir-atomic-code-swap.md

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

RELEASES_DIRNAME = "source-releases"
SOURCE_LINK = "source"


# meiva:note
# What a stage does with the release name it computed.
class StageAction(StrEnum):
    REUSE = "reuse"
    REBUILD = "rebuild"
    BUILD = "build"


# meiva:note
# What the home's `source` is before a switch: nothing, a pointer, or anything else (a checkout).
class LinkKind(StrEnum):
    ABSENT = "absent"
    POINTER = "pointer"
    OTHER = "other"


class StagePlanError(RuntimeError):
    pass


def releases_dir(home: Path) -> Path:
    return home / RELEASES_DIRNAME


def source_link(home: Path) -> Path:
    return home / SOURCE_LINK


# meiva:note
# A release's directory name: the code version first so a listing sorts by age, then the short
# commit a person recognises.
def release_id(commit: str, code_version: int) -> str:
    return f"{code_version}-{commit[:12]}"


# meiva:note
# A manifest is written last, so a manifest beside its tree means the release was finished and is
# reused once it verifies. Anything else under that name (a tree without a manifest, or a manifest
# whose tree is gone) is a stage or removal that stopped part-way and is rebuilt, unless the home's
# `source` points at it: that tree is never removed by a stage.
def stage_action(*, sealed: bool, present: bool, live: bool) -> StageAction:
    if sealed:
        return StageAction.REUSE
    if live:
        raise StagePlanError(
            "the home's source points at an unfinished release; refusing to rebuild it"
        )
    return StageAction.REBUILD if present else StageAction.BUILD


# meiva:note
# Why a switch must not happen, or None. Only a sealed, unchanged release of this home becomes the
# target, and only a pointer is replaced: a `source` that is a real checkout directory is moved
# aside by the one-time cutover, by hand.
def switch_refusal(*, link: LinkKind, own_release: bool, findings: Sequence[str]) -> str | None:
    if not own_release:
        return "the release is not a directory of this home's releases"
    if findings:
        return "the release differs from its manifest: " + "; ".join(findings[:5])
    if link is LinkKind.OTHER:
        return "source is a checkout, not a pointer: move it aside first (one-time cutover)"
    return None

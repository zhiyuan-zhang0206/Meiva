# meiva:note
# The decisions of one media registration (`meiva.self.attach`), over values: whether the
# agent's model can receive media at all, and which refusal a request earns once the caller has
# read the file facts. The registration shell (`attachment_transport.register_media`) reads the
# process, the file system and the model catalog and executes the answer. Nothing here reads
# or writes the process, so the order of the gates is tested as a table.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.lm.attach.constants import (
    ATTACH_MAX_FILE_BYTES,
    ATTACH_MAX_LABEL_CHARS,
    ATTACH_MEDIA_MIME,
)

ATTACH_CHANNEL = "attach"
_ATTACH_MEMBER = "meiva.self.attach"


# meiva:note
# Where a path lands: an absolute path as it is, a relative one under the caller's workspace
# base. The base is a value the caller read (the agent's workspace, or `$HOME` before an
# identity is bound) and `~` is expanded by the caller. The files namespace shares this rule.
def resolve_in(base: Path, expanded: Path) -> Path:
    return expanded if expanded.is_absolute() else base / expanded


@dataclass(frozen=True, slots=True)
class Refusal:
    error: type[Exception]
    message: str


@dataclass(frozen=True, slots=True)
class AttachFacts:
    model: str
    modalities: frozenset[str]
    in_turn: bool
    shown_path: str
    resolved_path: str
    is_file: bool
    suffix: str
    size: int
    label: object


def unavailable_reason(model: str, modalities: frozenset[str]) -> str | None:
    if modalities:
        return None
    return f"your model ({model}) is text-only and cannot receive media attachments"


def gated_members(reason: str | None) -> frozenset[str]:
    return frozenset() if reason is None else frozenset({_ATTACH_MEMBER})


def modality_of(suffix: str) -> str:
    mime = ATTACH_MEDIA_MIME[suffix]
    return "pdf" if mime == "application/pdf" else mime.split("/", maxsplit=1)[0]


def refusal(facts: AttachFacts) -> Refusal | None:
    if not facts.in_turn:
        return Refusal(
            RuntimeError,
            "meiva.self.attach only works inside an agent turn (execute_code); "
            "outside a turn there is no runner to deliver the attachment",
        )
    if not facts.is_file:
        return Refusal(
            FileNotFoundError,
            f"path {facts.shown_path!r} does not name an existing file ({facts.resolved_path})",
        )
    suffix = facts.suffix.lower()
    if suffix not in ATTACH_MEDIA_MIME:
        supported = ", ".join(sorted(ATTACH_MEDIA_MIME))
        return Refusal(
            ValueError,
            f"unsupported attachment suffix {facts.suffix!r}; supported suffixes: {supported}; "
            "text files belong in exec output or meiva.understand",
        )
    modality = modality_of(suffix)
    if modality not in facts.modalities:
        return Refusal(
            ValueError,
            f"attachment modality {modality!r} is not supported by your model ({facts.model}); "
            f"supported modalities: {', '.join(sorted(facts.modalities)) or 'none'}",
        )
    if facts.size > ATTACH_MAX_FILE_BYTES:
        return Refusal(
            ValueError, f"attachment exceeds the 20 MiB per-file limit: {facts.resolved_path}"
        )
    if not isinstance(facts.label, str | None):
        return Refusal(TypeError, "attachment label must be a str or None")
    if facts.label is not None and len(facts.label) > ATTACH_MAX_LABEL_CHARS:
        return Refusal(ValueError, f"attachment label exceeds {ATTACH_MAX_LABEL_CHARS} characters")
    return None


def appended_channel(
    existing: list[dict[str, str | None]], resolved_path: str, label: str | None
) -> list[dict[str, str | None]]:
    return [*existing, {"path": resolved_path, "label": label}]

# meiva:note
# One media registration (`meiva.self.attach`) decided over values, around the one read the shell
# makes (the file facts): `prepare_media` checks that the model can receive media at all and that
# the request is well typed; `base.paths.expand.expand_home` (the owner of what a leading `~` means)
# places it under the home the caller read; `decide_media` applies the refusals of
# `attach_plan.refusal` once the file facts are known and yields the entry to record;
# `registers_in_turn` says whether the process may deliver at all. Nothing here reads or writes the
# process.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from base.agents.context.identity import AgentIdentity
from base.lm.catalog import ModelCatalog
from meiva.sdk_surface.pure.media.attach.attach_model import modalities_for
from meiva.sdk_surface.pure.media.attach.attach_plan import AttachFacts, refusal, unavailable_reason
from meiva.sdk_surface.validation import coerce_str


@dataclass(frozen=True, slots=True)
class MediaRequest:
    path: str | Path
    label: str | None
    model: str
    modalities: frozenset[str]
    in_turn: bool


@dataclass(frozen=True, slots=True)
class FileFacts:
    resolved: Path
    is_file: bool
    size: int


# meiva:note
# Gate and validate one registration before any file is looked at: a model that cannot receive
# media refuses first, then `path` (a str or Path) and `label` (a str or None) are type-checked.
def prepare_media(
    path: str | Path,
    *,
    label: str | None,
    catalog: ModelCatalog,
    model: str,
    in_turn: bool,
) -> MediaRequest:
    modalities = modalities_for(model, catalog)
    if reason := unavailable_reason(model, modalities):
        raise RuntimeError(
            f"meiva.self.attach is unavailable: {reason}; switch to a vision-capable model"
        )
    return MediaRequest(
        path=coerce_str(path, "path", allow_types=(Path,)),
        label=coerce_str(label, "label", allow_none=True),
        model=model,
        modalities=modalities,
        in_turn=in_turn,
    )


# meiva:note
# The facts of the file a registration names, from its resolved path and what `stat` returned for
# it: `mode` and `size`, or None for both when the path cannot be stat'ed. Only a regular file has
# a size; a directory or a missing path has none.
def file_facts(resolved: Path, *, mode: int | None, size: int) -> FileFacts:
    is_file = mode is not None and stat.S_ISREG(mode)
    return FileFacts(resolved=resolved, is_file=is_file, size=size if is_file else 0)


# meiva:note
# The entry to record for `request` over the facts of its file, or the refusal
# `attach_plan.refusal` chose, raised.
def decide_media(request: MediaRequest, file: FileFacts) -> tuple[str, str | None]:
    refused = refusal(
        AttachFacts(
            model=request.model,
            modalities=request.modalities,
            in_turn=request.in_turn,
            shown_path=str(request.path),
            resolved_path=str(file.resolved),
            is_file=file.is_file,
            suffix=file.resolved.suffix,
            size=file.size,
            label=request.label,
        )
    )
    if refused is not None:
        raise refused.error(refused.message)
    return str(file.resolved), request.label


# meiva:note
# Whether a registration is made inside an exec turn: the process runs one and its identity is
# not borrowed through an external lease (a borrowed lease never runs a turn of its own).
def registers_in_turn(*, in_exec_turn: bool, identity: AgentIdentity | None) -> bool:
    borrowed = identity is not None and identity.lease is not None
    return in_exec_turn and not borrowed

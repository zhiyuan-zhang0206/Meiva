"""Register media for delivery through the exec turn's state update.

`meiva.self.attach` appends each registration to `meiva.state_update["attach"]`; the exec node takes
the entries out of the delta it commits, validates them, parks them in the `attach` channel of the
checkpointed state and turns them into one media message appended right after the exec output of the
registering turn (user ruling 2026-08-26). The SDK keeps no buffer of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from base.lm.catalog import ModelCatalog
from base.paths.expand import expand_home
from meiva.sdk_surface import process_context
from meiva.sdk_surface.pure.media.attach.attach_model import running_model
from meiva.sdk_surface.pure.media.attach.attach_plan import (
    ATTACH_CHANNEL,
    appended_channel,
    resolve_in,
)
from meiva.sdk_surface.pure.media.attach.attach_request import (
    FileFacts,
    decide_media,
    file_facts,
    prepare_media,
    registers_in_turn,
)


# meiva:note
# The facts of the file `candidate` names, read from the file system: its resolved path and its
# stat. A path that cannot be stat'ed has none.
def _probe(candidate: Path) -> FileFacts:
    resolved = candidate.resolve()
    try:
        st = resolved.stat()
    except OSError:
        return file_facts(resolved, mode=None, size=0)
    return file_facts(resolved, mode=st.st_mode, size=st.st_size)


# meiva:note
# One media registration over explicit inputs: the pure gate and validation, the pure home
# expansion, the file facts (the only read) and then the pure decision. `base` is the directory a
# relative `path` resolves against, `user_home` the home `~` expands against, `model` the agent's
# resolved model and `in_turn` whether this process runs an exec turn on its own identity (a
# borrowed lease never does). Returns the entry to record or raises the refusal `attach_plan` chose.
def plan_media(
    path: str | Path,
    *,
    label: str | None,
    base: Path,
    user_home: Path,
    catalog: ModelCatalog,
    model: str,
    in_turn: bool,
) -> tuple[str, str | None]:
    request = prepare_media(path, label=label, catalog=catalog, model=model, in_turn=in_turn)
    candidate = resolve_in(base, expand_home(request.path, user_home=user_home))
    return decide_media(request, _probe(candidate))


# meiva:note
# Append one registration to the exec turn's state-update dict, the channel the exec node takes
# the entries out of. `update` is that dict (anything else is a framework bug and raises).
def record_media(update: object, entry: tuple[str, str | None]) -> None:
    state: Any = update
    if not isinstance(update, dict):
        raise TypeError(
            f"meiva.state_update must stay a dict, got {type(update).__name__} (attachment)"
        )
    state[ATTACH_CHANNEL] = appended_channel(state.get(ATTACH_CHANNEL, []), *entry)


# meiva:note
# `meiva.self.attach` as the process sees it: read the catalog, the model, the turn and the
# state-update slot once, then register. `base` is the caller's workspace directory and
# `user_home` the home it read for `~`.
def register(path: str | Path, *, label: str | None, base: Path, user_home: Path) -> None:
    from meiva.sdk_surface import settings as sdk_settings

    bound = process_context.ambient_context()
    catalog = sdk_settings.model_catalog()
    entry = plan_media(
        path,
        label=label,
        base=base,
        user_home=user_home,
        catalog=catalog,
        model=running_model(sdk_settings.agent_setting("llm_model"), catalog=catalog),
        in_turn=registers_in_turn(
            in_exec_turn=process_context.in_exec_turn(),
            identity=None if bound is None else bound.identity,
        ),
    )
    record_media(process_context.get_state_update(), entry)

# meiva:note
# `understand_deps()` takes the directory relative paths resolve against and the output site from
# the bound context. The context here names a Meiva home that differs from the environment's, so
# deps built from the environment would point under `unit_home`.
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from meiva.sdk_surface import process_context
from meiva.sdk_surface.installation import Installation
from meiva.tests.fixtures.model_owner import model_installation as model_installation
from meiva.tests.fixtures.model_owner import sdk_model_owner as sdk_model_owner
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva.understand import wiring
from meiva.understand.answer import OVERFLOW_DIRNAME


def test_the_deps_sit_under_the_workspace_of_the_bound_contexts_home(
    tmp_path: Path, unit_home: Path, sdk_model_owner: Installation
) -> None:
    context_home = tmp_path / "context-home"
    pin_agent(7)
    bound = process_context.current_context()
    process_context.unbind_context()
    process_context.bind_context(replace(bound, meiva_home=context_home))
    assert context_home != unit_home

    deps = wiring.understand_deps()

    workspace = context_home / "workspaces" / "7"
    assert deps.base_dir == workspace
    assert deps.output is not None
    assert deps.output.directory == workspace / OVERFLOW_DIRNAME

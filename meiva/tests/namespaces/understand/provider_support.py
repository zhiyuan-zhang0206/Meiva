# meiva:note
# The harness the meiva.understand tests share: `meiva.understand` run over deps built from the
# test's configuration authority and catalog, with the text and media models scripted behind the
# real provider factory (the deps name the `llm_override` seam). Relative paths resolve against
# `tmp_path`, nothing is saved unless a test gives an output site, and findings are dropped
# unless a test gives an exec turn.

from __future__ import annotations

import dataclasses
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage

from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import model_overrides
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort
from meiva.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from meiva.security import FindingDelivery
from meiva.tests.fixtures.scripted_chat_model import OVERRIDE, Script, ScriptedModels
from meiva.understand import answer, plans, wiring
from meiva.understand.answer import OutputSite, UnderstandDeps

__all__ = [
    "UnderstandHarness",
    "fake_image",
    "fake_pdf",
    "fake_video",
    "run_understand",
    "understand_harness",
]

ANSWER = "fake answer"


def run_understand(
    deps: UnderstandDeps,
    targets: list[dict[str, Any]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]:
    return answer.run(plans.normalize(targets, effort, max_concurrent), deps)


# meiva:note
# `meiva.understand` over `deps`; `text` and `media` are the scripts of the two models.
class UnderstandHarness:
    def __init__(self, deps: UnderstandDeps, models: ScriptedModels) -> None:
        self.deps = deps
        self.scripted_models = models
        self.text = models.script(deps.text_model, ANSWER)
        self.media = models.script(deps.media_model, ANSWER)

    # meiva:note
    # Change fields of the deps; a renamed model keeps its script under the new name.
    def configure(self, **changes: Any) -> None:
        self.deps = dataclasses.replace(self.deps, **changes)
        self.scripted_models.bind(self.deps.text_model, self.text)
        self.scripted_models.bind(self.deps.media_model, self.media)

    def use_exec_turn(self, update: dict[str, Any]) -> None:
        self.configure(delivery=FindingDelivery(enabled=True, in_turn=True, update=update))

    def save_to(self, directory: Path) -> None:
        self.configure(output=OutputSite(directory, UTC))

    def understand(
        self,
        targets: list[dict[str, Any]],
        effort: str | ReasoningEffort = ReasoningEffort.MAX,
        max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
    ) -> list[str]:
        return run_understand(self.deps, targets, effort, max_concurrent)

    # meiva:note
    # The content blocks of the single HumanMessage of the script's latest request.
    @staticmethod
    def content_of(script: Script) -> list[Any]:
        [message] = script.requests[-1]
        assert isinstance(message, BaseMessage)
        content = message.content
        assert isinstance(content, list)
        return content


@pytest.fixture
def fake_image(tmp_path: Path) -> Path:
    path = tmp_path / "img.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return path


@pytest.fixture
def fake_video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 32)
    return path


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"\x00" * 32)
    return path


# meiva:note
# The deps of the test configuration: relative paths resolve against `tmp_path`, the answer
# models are scripted, nothing is saved and findings are dropped.
@pytest.fixture
def understand_harness(
    scripted_models: ScriptedModels,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    tmp_path: Path,
) -> UnderstandHarness:
    deps = wiring.understand_deps_for(
        config_authority,
        catalog=model_catalog,
        overrides=model_overrides(),
        base_dir=tmp_path,
        output=None,
        delivery=FindingDelivery(enabled=True, in_turn=False, update=None),
    )
    return UnderstandHarness(dataclasses.replace(deps, llm_override=OVERRIDE), scripted_models)

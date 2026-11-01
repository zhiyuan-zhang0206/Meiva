# meiva:note
# The harness the meiva.understand tests share: `meiva.understand` run over deps built from the
# test's configuration authority and catalog, with the text and media models scripted behind the
# real provider factory (the deps name the `llm_override` seam). Relative paths resolve against
# `tmp_path`, nothing is saved unless a test gives an output site, and findings are dropped
# unless a test binds an exec turn (`use_exec_turn`; the fixture unbinds it).

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import model_overrides
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort
from base.packages.tests.model_catalog import inert_keys
from meiva.sdk_surface import process_context
from meiva.sdk_surface.pure.media.finding_plan import FindingDelivery
from meiva.sdk_surface.pure.plans.batch_plan import DEFAULT_BATCH_MAX_CONCURRENT
from meiva.tests.fixtures.scripted_chat_model import OVERRIDE, Script, ScriptedModels
from meiva.understand import answer, plans, wiring
from meiva.understand.answer import OutputSite, UnderstandDeps

__all__ = [
    "UnderstandHarness",
    "fake_image",
    "fake_pdf",
    "fake_video",
    "image_block",
    "run_understand",
    "understand_harness",
]

ANSWER = "fake answer"


# meiva:note
# The block an image reaches a model in: a standard `image_url` data URI, whatever the provider.
def image_block(data: bytes, mime: str = "image/png") -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def run_understand(
    deps: UnderstandDeps,
    targets: list[dict[str, Any]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]:
    delivery = FindingDelivery(enabled=True, in_turn=process_context.in_exec_turn())
    return answer.run(plans.normalize(targets, effort, max_concurrent), deps, delivery)


# meiva:note
# `meiva.understand` over `deps`; each configured model has one script, shared by the content types
# that name the same model (by default text and image share the deepseek-flash script, and pdf,
# audio and video the Gemini one). `text`, `image`, `pdf`, `audio` and `video` are the scripts of
# the models configured for those types.
class UnderstandHarness:
    def __init__(self, deps: UnderstandDeps, models: ScriptedModels) -> None:
        self.deps = deps
        self.scripted_models = models
        self._scripts: dict[str, Script] = {}
        self._bind_models()

    def _bind_models(self) -> None:
        for model in dict.fromkeys(dataclasses.astuple(self.deps.models)):
            if model not in self._scripts:
                self._scripts[model] = self.scripted_models.script(model, ANSWER)
            self.scripted_models.bind(model, self._scripts[model])

    @property
    def text(self) -> Script:
        return self._scripts[self.deps.models.text]

    @property
    def image(self) -> Script:
        return self._scripts[self.deps.models.image]

    @property
    def pdf(self) -> Script:
        return self._scripts[self.deps.models.pdf]

    @property
    def audio(self) -> Script:
        return self._scripts[self.deps.models.audio]

    @property
    def video(self) -> Script:
        return self._scripts[self.deps.models.video]

    # meiva:note
    # Change fields of the deps; a model named for the first time gets its own script.
    def configure(self, **changes: Any) -> None:
        self.deps = dataclasses.replace(self.deps, **changes)
        self._bind_models()

    # meiva:note
    # Configure the model of some content types (`text`, `image`, `pdf`, `audio`, `video`).
    def configure_models(self, **changes: str) -> None:
        self.configure(models=dataclasses.replace(self.deps.models, **changes))

    # meiva:note
    # Bind an exec turn in this process and return its state update, where the findings land.
    @staticmethod
    def use_exec_turn() -> dict[str, Any]:
        update: dict[str, Any] = {}
        process_context.set_state(object())
        process_context.set_state_update(update)
        return update

    def save_to(self, directory: Path) -> None:
        timers = ManualTimers()
        clock = Clock(
            ClockConfig("UTC", "UTC", message_timestamp_weekday=False),
            timers=timers,
            now=timers.now,
        )
        self.configure(output=OutputSite(directory, clock))

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
) -> Iterator[UnderstandHarness]:
    deps = wiring.understand_deps_for(
        config_authority,
        catalog=model_catalog,
        keys=inert_keys(model_catalog),
        overrides=model_overrides(),
        base_dir=tmp_path,
        output=None,
    )
    yield UnderstandHarness(dataclasses.replace(deps, llm_override=OVERRIDE), scripted_models)
    process_context.unbind_exec_turn()

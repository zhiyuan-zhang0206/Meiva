# meiva:note
# Answer one prompt per target: read the files, call the provider, scan what goes in and comes
# out, and save the answers. `meiva/understand/__init__.py` is the namespace function; it
# validates its arguments (`plans.py`), builds the `UnderstandDeps` of this process (`wiring.py`)
# and calls `run`. What each call is (`calls.py`) and what is saved (`outputs.py`) are decided
# from values; this shell builds the model through the real provider factory, reads and writes
# the files, and returns the findings of what goes in and out next to the answers.
# meiva:link meiva/understand/docs/understand.meiva.okf.md

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from base.clock import Clock
from base.host.env.agent_slices import ModelOverrides
from base.lm import factory
from base.lm.call import invoke_text
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.effort import ReasoningEffort
from base.lm.usage.metering import unmeter
from meiva import security
from meiva.sdk_surface.batch import run_batch
from meiva.sdk_surface.pure.media.attach_plan import resolve_in
from meiva.sdk_surface.pure.media.finding_plan import (
    CarriedFindingsError,
    FindingDelivery,
    Findings,
    Scanned,
    findings_of,
    gather,
)
from meiva.understand import calls, outputs
from meiva.understand.calls import MediaSettings, ModelCall
from meiva.understand.errors import UnderstandError
from meiva.understand.plans import Plan, TargetSpec

__all__ = [
    "OVERFLOW_DIRNAME",
    "OutputSite",
    "RetryPolicy",
    "UnderstandDeps",
    "run",
]

OVERFLOW_DIRNAME = ".exec_output"
_OVERFLOW_KEEP = 20


# meiva:note
# How a failed provider call is retried.
@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    delay_seconds: float
    max_delay_seconds: float


# meiva:note
# Where answers are saved and the clock whose zone their timestamps use; absent outside an agent
# process.
@dataclass(frozen=True)
class OutputSite:
    directory: Path
    clock: Clock


# meiva:note
# Everything `run` reads that is not its plan: the model facts, the configured models and knobs,
# the directory relative paths resolve against, and where answers are saved.
@dataclass(frozen=True)
class UnderstandDeps:
    catalog: ModelCatalog
    keys: ProviderKeys
    overrides: ModelOverrides
    text_model: str
    media_model: str
    media_resolution: str
    media_thinking_level: str | None
    media_base_url: str | None
    llm_override: str
    timeout_seconds: float
    retry: RetryPolicy
    base_dir: Path
    output: OutputSite | None


# meiva:note
# Answer every target of the plan, in input order, and save each answer. The findings of every
# input and answer are handed to `meiva.security.deliver` once, with the process facts the caller
# read (`delivery`), also when a target fails after its input was scanned or a save fails. The
# per-invoke bound lives in the provider client (the chat model's timeout) rather than a
# batch-layer wait_for: asyncio.run's loop close waits for to_thread executor threads, so a
# wait_for timeout would not return until the wedged SDK call finished anyway. The SDK timeout
# makes the worker thread end on its own, and invoke_text's retry restarts the bounded call.
def run(plan: Plan, deps: UnderstandDeps, delivery: FindingDelivery) -> list[str]:
    try:
        answered = gather(
            asyncio.run(
                run_batch(
                    list(plan.targets),
                    lambda t: _understand_one(
                        deps,
                        prompt=t.prompt,
                        text=t.text,
                        paths=t.paths,
                        effort=plan.effort,
                    ),
                    plan.max_concurrent,
                )
            )
        )
    except CarriedFindingsError as failed:
        error = security.deliver_kept(delivery, failed)
    else:
        findings: Findings = (
            *answered.findings,
            *(
                finding
                for result in answered.value
                for finding in findings_of(result, "understand.output")
            ),
        )
        try:
            for target, result in zip(plan.targets, answered.value, strict=True):
                _save_result(deps, target, result)
        finally:
            security.deliver(delivery, findings)
        return answered.value
    raise error


# meiva:note
# Synchronous unit of work, one prompt against one source; split out so the asyncio layer can
# schedule multiple copies via `asyncio.to_thread`. The findings of the input ride with the answer,
# and with the failure when reading or answering fails after the scan.
def _understand_one(
    deps: UnderstandDeps,
    *,
    prompt: str,
    text: str | None,
    paths: tuple[str | Path, ...] | None,
    effort: str | ReasoningEffort,
) -> Scanned[str]:
    if text is not None:
        findings = findings_of(text, "understand.input")
        try:
            answer = _call_text(
                deps,
                [{"type": "text", "text": text}, {"type": "text", "text": prompt}],
                effort=effort,
            )
        except Exception as error:
            raise CarriedFindingsError(error, findings) from error
        return Scanned(answer, findings)
    assert paths is not None  # noqa: S101 — validated by caller
    return _understand_paths(deps, paths, prompt, effort=effort)


# meiva:note
# Read `p` as UTF-8 text; undecodable bytes raise UnderstandError with the supported-suffix hint.
def _read_text_file(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise UnderstandError(
            f"Could not read {str(p)!r} as UTF-8 text ({e}). "
            f"If this is binary media, its extension {p.suffix!r} is not recognized — "
            f"supported media suffixes: {calls.supported_media_suffixes()}."
        ) from e


# meiva:note
# Read the file `path` names as one content part and its mime type: a media file (image, video,
# audio, PDF by suffix) as a media part, any other file as UTF-8 text, which is also scanned.
def _read_part(deps: UnderstandDeps, path: str | Path) -> Scanned[tuple[dict[str, Any], str]]:
    p = resolve_in(deps.base_dir, Path(path).expanduser())
    if not p.is_file():
        raise FileNotFoundError(f"path {str(path)!r} does not name an existing file ({p})")
    mime = calls.media_mime(p.suffix)
    if mime is not None:
        data = p.read_bytes()
        calls.check_inline_size(len(data))
        return Scanned(({"type": "media", "data": data, "mime_type": mime}, mime))
    content = _read_text_file(p)
    return Scanned(
        ({"type": "text", "text": content}, "text/plain"),
        findings_of(content, "understand.input"),
    )


# meiva:note
# Answer `prompt` over several files in ONE model call. Media files become media parts; any
# other file is read as UTF-8 text and becomes a text part. If at least one part is media the
# whole call runs on the media model (the media provider handles text parts natively); an
# all-text list stays on the text model. Part order follows `paths`, prompt last. The findings of
# the files read so far ride with the answer, and with the failure when reading or answering
# fails.
def _understand_paths(
    deps: UnderstandDeps,
    paths: tuple[str | Path, ...],
    prompt: str,
    effort: str | ReasoningEffort,
) -> Scanned[str]:
    parts: list[Any] = []
    mimes: list[str] = []
    found: Findings = ()
    try:
        for path in paths:
            read = _read_part(deps, path)
            parts.append(read.value[0])
            mimes.append(read.value[1])
            found = (*found, *read.findings)
        parts.append({"type": "text", "text": prompt})
        if any(m != "text/plain" for m in mimes):
            answer = _call_media(deps, parts, mime=",".join(dict.fromkeys(mimes)), effort=effort)
        else:
            answer = _call_text(deps, parts, effort=effort)
    except Exception as error:
        raise CarriedFindingsError(error, found) from error
    return Scanned(answer, found)


# meiva:note
# Save one target's result under `.exec_output/` of the agent workspace. Saving runs after the
# batch returned, in input order, so the timestamped filenames cannot interleave and the saved
# files sort the way the batch was submitted.
def _save_result(deps: UnderstandDeps, target: TargetSpec, result: str) -> None:
    site = deps.output
    if site is None:
        return
    path_strs = [str(p) for p in target.paths] if target.paths is not None else None
    saved = outputs.output_file(
        target.prompt,
        result,
        outputs.result_source(path_strs),
        site.clock.now().astimezone(site.clock.explicit_zone()),
    )
    site.directory.mkdir(parents=True, exist_ok=True)
    (site.directory / saved.name).write_text(saved.content, encoding="utf-8")
    existing = [f.name for f in site.directory.glob("understand_*.txt")]
    for stale in outputs.stale_outputs(existing, _OVERFLOW_KEEP):
        (site.directory / stale).unlink(missing_ok=True)


def _call_text(deps: UnderstandDeps, content: list[Any], *, effort: str | ReasoningEffort) -> str:
    return _invoke(
        deps, calls.plan_text_call(deps.text_model, deps.timeout_seconds, effort), content
    )


def _call_media(
    deps: UnderstandDeps, content: list[Any], *, mime: str, effort: str | ReasoningEffort
) -> str:
    call = calls.plan_media_call(
        deps.catalog,
        deps.media_model,
        mime=mime,
        timeout=deps.timeout_seconds,
        effort=effort,
        settings=MediaSettings(
            deps.media_resolution, deps.media_thinking_level, deps.media_base_url
        ),
    )
    return _invoke(deps, call, content)


# meiva:note
# Build the planned model through the real provider factory and invoke it once on `content`. A
# build failure (missing API key etc.) surfaces as `UnderstandError`.
def _invoke(deps: UnderstandDeps, call: ModelCall, content: list[Any]) -> str:
    options = call.build
    try:
        llm = factory.build_chat_model(
            call.model,
            catalog=deps.catalog,
            keys=deps.keys,
            llm_override=deps.llm_override,
            overrides=deps.overrides,
            reasoning_effort=options.reasoning_effort,
            timeout=options.timeout,
            media_resolution=options.media_resolution,
            media_thinking_level=options.media_thinking_level,
            base_url=options.base_url,
        )
    except RuntimeError as e:
        raise UnderstandError(str(e)) from e
    return invoke_text(
        unmeter(llm),
        content,
        desc=call.desc,
        error_type=UnderstandError,
        retry_attempts=deps.retry.attempts,
        retry_delay_seconds=deps.retry.delay_seconds,
        retry_max_delay_seconds=deps.retry.max_delay_seconds,
        model=call.model,
        usage_source=call.usage_source,
        catalog=deps.catalog,
    )

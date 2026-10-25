"""Validate the arguments of `meiva.understand` and turn them into a `Plan`, with no ambient reads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from base.lm.effort import ReasoningEffort, coerce_effort
from meiva.sdk_surface.batch import validate_max_concurrent
from meiva.sdk_surface.validation import coerce_str

__all__ = ["Plan", "TargetSpec", "normalize"]


def _is_object_list(val: object) -> TypeGuard[list[object]]:
    return isinstance(val, list)


def _is_str_dict(val: object) -> TypeGuard[dict[str, object]]:
    return isinstance(val, dict)


@dataclass(frozen=True, slots=True)
class TargetSpec:
    prompt: str
    text: str | None = None
    paths: tuple[str | Path, ...] | None = None


@dataclass(frozen=True)
class Plan:
    """What one `meiva.understand` call answers: its targets, reasoning effort and concurrency."""

    targets: tuple[TargetSpec, ...]
    effort: str | ReasoningEffort
    max_concurrent: int | None


def _parse_paths(i: int, raw_paths: object) -> tuple[str | Path, ...]:
    if not _is_object_list(raw_paths):
        raise TypeError(
            f"targets[{i}]['paths'] must be a list of file paths, "
            f"got {type(raw_paths).__name__}. "
            "Example: {'prompt': 'compare these', "
            "'paths': ['design.png', 'shot.png']}"
        )
    if not raw_paths:
        raise ValueError(
            f"targets[{i}]['paths'] must not be empty — pass at least one file. "
            "Example: {'prompt': 'compare these', "
            "'paths': ['design.png', 'shot.png']}"
        )
    checked_paths: list[str | Path] = []
    for j, p in enumerate(raw_paths):
        if not isinstance(p, (str, Path)):
            raise TypeError(
                f"targets[{i}]['paths'][{j}] must be a path string or Path, got {type(p).__name__}"
            )
        checked_paths.append(p)
    return tuple(checked_paths)


def _parse_target(i: int, raw_t: object) -> TargetSpec:
    if not _is_str_dict(raw_t):
        raise TypeError(
            f"targets[{i}] must be a dict, got {type(raw_t).__name__}. "
            "Example: {'prompt': 'what is in this image', 'paths': ['shot.png']}"
        )
    if "prompt" not in raw_t:
        raise ValueError(
            f"targets[{i}] missing required key 'prompt'. "
            "Example: {'prompt': 'summarize this', 'text': 'the material'}"
        )
    raw_prompt = raw_t["prompt"]
    prompt = raw_prompt if isinstance(raw_prompt, str) else str(raw_prompt)
    srcs = [k for k in ("text", "paths") if k in raw_t]
    if len(srcs) != 1:
        raise ValueError(
            f"targets[{i}] must have exactly one of 'text' / 'paths', got {srcs}. "
            "Example: {'prompt': 'summarize this', 'paths': ['notes.md']}"
        )
    if "paths" in raw_t:
        return TargetSpec(prompt=prompt, paths=_parse_paths(i, raw_t["paths"]))
    raw_text = raw_t["text"]
    return TargetSpec(
        prompt=prompt,
        text=raw_text if isinstance(raw_text, str) else str(raw_text),
    )


def normalize(targets: object, effort: str | ReasoningEffort, max_concurrent: int | None) -> Plan:
    """Check the targets and coerce the effort; raise on the first invalid input."""
    if not _is_object_list(targets):
        raise TypeError(
            f"understand() takes a list of target dicts, got {type(targets).__name__}. "
            "Example: meiva.understand([{'prompt': 'summarize this', 'paths': ['notes.md']}])"
        )
    parsed_targets = [_parse_target(i, raw_t) for i, raw_t in enumerate(targets)]
    effort = coerce_str(effort, "effort")
    normalized = coerce_effort(effort, example="meiva.understand(targets, effort='low')")
    assert normalized is not None  # noqa: S101 — understand's default is MAX, never None
    validate_max_concurrent(max_concurrent, example="meiva.understand(targets, max_concurrent=4)")
    return Plan(
        targets=tuple(parsed_targets),
        effort=normalized,
        max_concurrent=max_concurrent,
    )

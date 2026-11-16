# meiva:link base/lm/docs/decision-models.md

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

from base.lm.catalog.decide.types import (
    FALSE,
    TRUE,
    Choice,
    Decision,
    DecisionResponseError,
    Question,
    Score,
    YesNo,
)


@dataclass(frozen=True)
class WireQuestions:
    payload: dict[str, dict[str, object]]
    by_wire_id: dict[str, tuple[str, Question]]


# meiva:note
# Google's CreateDecisionRequest.questions keys must match ^[A-Za-z0-9_-]{1,64}$, whereas
# callers key questions by domain identifiers such as colon-delimited sub-skill names or
# paths over 64 characters. Assign sequential wire IDs q0..q{n-1} in iteration order so
# every caller key set maps bijectively onto valid wire keys without collisions.
# meiva:link base/lm/docs/decision-models.md
def encode_questions(questions: Mapping[str, Question]) -> WireQuestions:
    payload: dict[str, dict[str, object]] = {}
    by_wire_id: dict[str, tuple[str, Question]] = {}
    for index, (caller_id, question) in enumerate(questions.items()):
        if not caller_id.strip():
            raise ValueError("Gemini Decisions question ids must be non-empty strings")
        wire_id = f"q{index}"
        payload[wire_id] = wire_question(question)
        by_wire_id[wire_id] = (caller_id, question)
    return WireQuestions(payload=payload, by_wire_id=by_wire_id)


def wire_question(question: Question) -> dict[str, object]:
    match question:
        case YesNo():
            return {
                "type": "noul",
                "instructions": question.question,
                "criteria": {
                    "true_description": question.true_if,
                    "false_description": question.false_if,
                },
            }
        case Choice():
            return {
                "type": "choice",
                "instructions": question.question,
                "criteria": dict(question.options),
            }
        case Score():
            return {
                "type": "score",
                "instructions": question.question,
                "criteria": [
                    f"{label}: {description}"
                    for label, description in zip(question.scale, question.rubric, strict=True)
                ],
            }


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _object(value: object, what: str) -> dict[str, object]:
    if not _is_str_dict(value):
        raise DecisionResponseError(f"Gemini Decisions response: {what} is not an object")
    return value


def input_tokens(body: Mapping[str, object]) -> int:
    usage = body.get("usage")
    tokens = usage.get("total_input_tokens") if _is_str_dict(usage) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise DecisionResponseError(
            "Gemini Decisions response carries no valid usage.total_input_tokens"
        )
    return tokens


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DecisionResponseError(f"Gemini Decisions response: {what} is not a number: {value!r}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise DecisionResponseError(f"Gemini Decisions response: {what} is not finite") from exc
    if not math.isfinite(number):
        raise DecisionResponseError(f"Gemini Decisions response: {what} is not finite")
    return number


def parse_answers(body: Mapping[str, object], encoded: WireQuestions) -> dict[str, Decision]:
    answers = _object(body.get("answers"), "answers")
    missing = [
        f"{caller_id!r} (wire {wire_id!r})"
        for wire_id, (caller_id, _) in encoded.by_wire_id.items()
        if wire_id not in answers
    ]
    unknown = sorted(answers.keys() - encoded.by_wire_id.keys())
    if missing or unknown:
        raise DecisionResponseError(
            "Gemini Decisions answer keys do not match wire questions: "
            f"missing [{', '.join(missing)}], unknown {unknown}"
        )
    decisions: dict[str, Decision] = {}
    for wire_id, (caller_id, question) in encoded.by_wire_id.items():
        label = f"{caller_id!r} (wire {wire_id!r})"
        decisions[caller_id] = _parse_answer(
            label, question, _object(answers[wire_id], f"answer for {label}")
        )
    return decisions


def _parse_answer(label: str, question: Question, answer: Mapping[str, object]) -> Decision:
    expected = {YesNo: "noul", Choice: "choice", Score: "score"}[type(question)]
    if answer.get("type") != expected:
        raise DecisionResponseError(
            f"Gemini Decisions answer for {label} has type {answer.get('type')!r}, "
            f"expected {expected!r}"
        )
    if isinstance(question, YesNo):
        p_true = _number(answer.get("noul"), f"{label}.noul")
        return Decision(
            top=TRUE if p_true >= 0.5 else FALSE,
            probabilities={TRUE: p_true, FALSE: 1.0 - p_true},
        )
    raw = _object(answer.get("probabilities"), f"{label}.probabilities")
    confidence = _number(answer.get("confidence"), f"{label}.confidence")
    if isinstance(question, Choice):
        top = answer.get("choice")
        if not isinstance(top, str):
            raise DecisionResponseError(f"Gemini Decisions answer for {label} carries no choice")
        probabilities = {str(k): _number(v, f"{label}.probabilities[{k}]") for k, v in raw.items()}
        return Decision(top=top, probabilities=probabilities, confidence=confidence)
    by_label: dict[str, float] = {}
    for key, value in raw.items():
        if key not in {str(i) for i in range(len(question.scale))}:
            raise DecisionResponseError(
                f"Gemini Decisions answer for {label} has level key {key!r} "
                f"outside the {len(question.scale)} levels"
            )
        scale_label = question.scale[int(key)]
        by_label[scale_label] = _number(value, f"{label}.probabilities[{key}]")
    if not by_label:
        raise DecisionResponseError(
            f"Gemini Decisions answer for {label} carries no score probabilities"
        )
    return Decision(
        top=max(by_label, key=lambda item: by_label[item]),
        probabilities=by_label,
        confidence=confidence,
        score=_number(answer.get("score"), f"{label}.score"),
    )

# meiva:link meiva_builtins/plugins/lm_typesafe/docs/systemone/systemone.meiva.okf.md

from __future__ import annotations

import math
from collections.abc import Mapping
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


def wire_question(question: Question) -> dict[str, object]:
    match question:
        case YesNo():
            return {
                "type": "noul",
                "instructions": question.question,
                "criteria": {"true": question.true_if, "false": question.false_if},
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
        raise DecisionResponseError(f"SystemOne response: {what} is not an object")
    return value


def input_tokens(body: Mapping[str, object]) -> int:
    usage = body.get("usage")
    tokens = usage.get("input_tokens") if _is_str_dict(usage) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise DecisionResponseError("SystemOne response carries no valid usage.input_tokens")
    return tokens


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DecisionResponseError(f"SystemOne response: {what} is not a number: {value!r}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise DecisionResponseError(f"SystemOne response: {what} is not finite") from exc
    if not math.isfinite(number):
        raise DecisionResponseError(f"SystemOne response: {what} is not finite")
    return number


def parse_answers(
    body: Mapping[str, object], questions: Mapping[str, Question]
) -> dict[str, Decision]:
    answers = _object(body.get("answers"), "answers")
    decisions: dict[str, Decision] = {}
    for qid, answer in answers.items():
        question = questions.get(qid)
        if question is None:
            raise DecisionResponseError(f"SystemOne answered unknown question {qid!r}")
        decisions[qid] = _parse_answer(qid, question, _object(answer, f"answer {qid!r}"))
    return decisions


def _parse_answer(qid: str, question: Question, answer: Mapping[str, object]) -> Decision:
    expected = {YesNo: "noul", Choice: "choice", Score: "score"}[type(question)]
    if answer.get("type") != expected:
        raise DecisionResponseError(
            f"SystemOne answer {qid!r} has type {answer.get('type')!r}, expected {expected!r}"
        )
    if isinstance(question, YesNo):
        p_true = _number(answer.get("noul"), f"{qid}.noul")
        return Decision(
            top=TRUE if p_true >= 0.5 else FALSE,
            probabilities={TRUE: p_true, FALSE: 1.0 - p_true},
        )
    raw = _object(answer.get("probabilities"), f"{qid}.probabilities")
    confidence = _number(answer.get("confidence"), f"{qid}.confidence")
    if isinstance(question, Choice):
        top = answer.get("choice")
        if not isinstance(top, str):
            raise DecisionResponseError(f"SystemOne answer {qid!r} carries no choice")
        probabilities = {str(k): _number(v, f"{qid}.probabilities[{k}]") for k, v in raw.items()}
        return Decision(top=top, probabilities=probabilities, confidence=confidence)
    by_label: dict[str, float] = {}
    for key, value in raw.items():
        if key not in {str(i) for i in range(len(question.scale))}:
            raise DecisionResponseError(
                f"SystemOne answer {qid!r} has level key {key!r} outside the {len(question.scale)} levels"
            )
        label = question.scale[int(key)]
        by_label[label] = _number(value, f"{qid}.probabilities[{key}]")
    if not by_label:
        raise DecisionResponseError(f"SystemOne answer {qid!r} carries no score probabilities")
    return Decision(
        top=max(by_label, key=lambda label: by_label[label]),
        probabilities=by_label,
        confidence=confidence,
        score=_number(answer.get("score"), f"{qid}.score"),
    )

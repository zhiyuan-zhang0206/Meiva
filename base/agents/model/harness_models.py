from __future__ import annotations

import re
from collections.abc import Container, Mapping, Sequence
from typing import NamedTuple, TypeGuard

from base.api_contracts.agent_contract import (
    Harness,
    InvalidModelConfig,
    ProbedHarnessModel,
)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_AGY_MODEL_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_AGY_EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_AGY_EFFORT_RANK = {level: index for index, level in enumerate(_AGY_EFFORT_ORDER)}


class CatalogProbeResult(NamedTuple):
    models: tuple[ProbedHarnessModel, ...] = ()
    error: str | None = None
    signed_in: bool | None = None


_CODEX_TERMINAL_AUTH_MARKERS = (
    "refresh token was already used",
    "refresh_token_reused",
    "refresh token has expired",
    "refresh_token_expired",
    "refresh token was revoked",
    "refresh_token_invalidated",
    "your access token could not be refreshed",
    "please log out and sign in again",
    "please sign in again",
    "plan type is required for chatgpt authentication",
    "invalid_grant",
)
_CODEX_NETWORK_ERROR_MARKERS = (
    "dns error",
    "connection refused",
    "timed out",
    "failed to connect",
    "network is unreachable",
)


def _clean_codex_stderr_auth_line(line: str) -> str:
    for marker in ("Your access token could not be refreshed", "Failed to refresh token: "):
        index = line.find(marker)
        if index != -1:
            detail = line[index:].removeprefix("Failed to refresh token: ").strip()
            if detail:
                return detail
    return line.strip()


def _extract_codex_auth_failure(account_error: str | None, stderr_text: str) -> str | None:
    if account_error:
        low_error = account_error.lower()
        if any(marker in low_error for marker in _CODEX_TERMINAL_AUTH_MARKERS):
            return account_error.strip()
    for line in stderr_text.splitlines():
        low_line = line.lower()
        if any(marker in low_line for marker in _CODEX_TERMINAL_AUTH_MARKERS):
            return _clean_codex_stderr_auth_line(line)
    return None


# meiva:note
# Classifies a Codex app-server `account/read` and stderr probe: terminal refresh-token rejections
# and missing accounts under `requiresOpenaiAuth` prove the session is signed out (`False`), while
# transient network errors or unsupported `account/read` methods stay unproven (`None`).
# meiva:link base/agents/docs/harness/availability.md
def classify_codex_account_probe(
    account_result: object,
    account_error: str | None,
    account_error_code: int | None,
    stderr_text: str,
) -> tuple[bool | None, str | None]:
    failure = _extract_codex_auth_failure(account_error, stderr_text)
    if failure is not None:
        return False, failure
    if account_error is not None:
        if account_error_code == -32601:
            return None, None
        return None, f"codex account/read failed: {account_error}"
    if _is_mapping(account_result):
        account = account_result.get("account")
        if _is_mapping(account):
            return True, None
        if account is None and account_result.get("requiresOpenaiAuth") is True:
            low_stderr = stderr_text.lower()
            if any(marker in low_stderr for marker in _CODEX_NETWORK_ERROR_MARKERS):
                return None, "codex could not reach auth service"
            return False, "codex app-server reports no active account"
    return None, None


def _is_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return None


def _dedupe_strings(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return tuple(ordered)


def _choose_effort_default(
    efforts: tuple[str, ...], preferred: object, *, fallbacks: Sequence[str] = ()
) -> str | None:
    if not efforts:
        return None
    candidate = _non_empty_str(preferred)
    if candidate is not None and candidate in efforts:
        return candidate
    for level in fallbacks:
        if level in efforts:
            return level
    return efforts[0]


def _extract_codex_efforts(raw_efforts: object) -> tuple[str, ...]:
    if not _is_list(raw_efforts):
        return ()
    names: list[str] = []
    for entry in raw_efforts:
        if isinstance(entry, str):
            names.append(entry)
        elif _is_mapping(entry):
            level = _non_empty_str(entry.get("reasoningEffort"))
            if level is not None:
                names.append(level)
    return _dedupe_strings(names)


def _parse_codex_item(item: object) -> tuple[ProbedHarnessModel, bool] | None:
    if not _is_mapping(item) or item.get("hidden") is True:
        return None
    model_id = _non_empty_str(item.get("id")) or _non_empty_str(item.get("model"))
    if model_id is None:
        return None
    efforts = _extract_codex_efforts(item.get("supportedReasoningEfforts"))
    default_effort = _choose_effort_default(efforts, item.get("defaultReasoningEffort"))
    model = ProbedHarnessModel(
        id=model_id,
        reasoning_effort_options=efforts,
        reasoning_effort_default=default_effort,
    )
    return model, item.get("isDefault") is True


def parse_codex_model_list(pages: Sequence[object]) -> CatalogProbeResult:
    models: list[ProbedHarnessModel] = []
    seen: set[str] = set()

    for page in pages:
        if not _is_mapping(page):
            return CatalogProbeResult(error="codex model/list returned a non-object response")
        raw_items = page.get("data")
        if not _is_list(raw_items):
            return CatalogProbeResult(error="codex model/list returned no data array")
        for item in raw_items:
            parsed = _parse_codex_item(item)
            if parsed is None or parsed[0].id in seen:
                continue
            model, _is_default = parsed
            seen.add(model.id)
            models.append(model)

    if not models:
        return CatalogProbeResult(error="codex model/list returned no visible models")
    return CatalogProbeResult(models=tuple(models))


def _strip_one_million_suffix(model_id: str) -> str:
    return model_id[:-4] if model_id.lower().endswith("[1m]") else model_id


# meiva:note
# One initialize entry names every model ID the CLI runs for it: the resolved model, and also the
# `[1m]` selector when the entry carries one. `<id>[1m]` selects model `<id>` with the 1M-token
# context window, so the bare `<id>` is runnable too — Claude Code 2.1.304 even reports the
# suffixed form as `resolvedModel`, which is why the suffix is stripped from it.
def _claude_model_ids(entry: Mapping[str, object]) -> tuple[str, ...]:
    value = _non_empty_str(entry.get("value"))
    resolved = _non_empty_str(entry.get("resolvedModel"))
    ids: list[str] = []
    if value is not None and value.lower().endswith("[1m]"):
        ids.append(value)
    if resolved is not None:
        ids.append(_strip_one_million_suffix(resolved))
    elif value is not None and value != "default":
        ids.append(_strip_one_million_suffix(value))
    return _dedupe_strings(ids)


def _claude_entry_models(entry: Mapping[str, object]) -> tuple[ProbedHarnessModel, ...]:
    model_ids = _claude_model_ids(entry)
    raw_levels = entry.get("supportedEffortLevels")
    efforts = (
        _dedupe_strings([item for item in raw_levels if isinstance(item, str)])
        if entry.get("supportsEffort") is not False and _is_list(raw_levels)
        else ()
    )
    if not efforts:
        return tuple(ProbedHarnessModel(id=model_id) for model_id in model_ids)
    default_effort = _choose_effort_default(
        efforts, entry.get("defaultEffort"), fallbacks=("high", "medium")
    )
    return tuple(
        ProbedHarnessModel(
            id=model_id,
            reasoning_effort_options=efforts,
            reasoning_effort_default=default_effort,
        )
        for model_id in model_ids
    )


def _unwrap_claude_init_body(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object] | None, str | None]:
    if payload.get("type") != "control_response":
        return payload, None
    outer = payload.get("response")
    if not _is_mapping(outer):
        return None, "claude initialize returned malformed control_response"
    if outer.get("subtype") == "error":
        detail = _non_empty_str(outer.get("error"))
        return None, detail or "claude initialize control_request failed"
    nested = outer.get("response")
    return (nested if _is_mapping(nested) else outer), None


def _merge_claude_model(
    by_id: dict[str, ProbedHarnessModel], order: list[str], parsed: ProbedHarnessModel
) -> None:
    existing = by_id.get(parsed.id)
    if existing is None:
        by_id[parsed.id] = parsed
        order.append(parsed.id)
    elif not existing.reasoning_effort_options and parsed.reasoning_effort_options:
        by_id[parsed.id] = parsed


def parse_claude_initialize(payload: object) -> CatalogProbeResult:
    if not _is_mapping(payload):
        return CatalogProbeResult(error="claude initialize returned non-JSON payload")
    inner, error = _unwrap_claude_init_body(payload)
    if error is not None or inner is None:
        return CatalogProbeResult(error=error)
    raw_models = inner.get("models")
    if not _is_list(raw_models):
        return CatalogProbeResult(error="claude initialize returned no models list")

    by_id: dict[str, ProbedHarnessModel] = {}
    order: list[str] = []
    for item in raw_models:
        if not _is_mapping(item):
            continue
        for parsed in _claude_entry_models(item):
            _merge_claude_model(by_id, order, parsed)

    if not order:
        return CatalogProbeResult(error="claude initialize returned no usable models")
    models = tuple(by_id[model_id] for model_id in order)
    return CatalogProbeResult(models=models)


def _extract_agy_cli_id(line: str) -> str | None:
    cleaned = _ANSI_ESCAPE_RE.sub("", line).rsplit("\r", 1)[-1].strip()
    if not cleaned:
        return None
    if "\t" in cleaned:
        token = cleaned.split("\t", 1)[0].strip()
    else:
        parts = re.split(r"\s{2,}", cleaned, maxsplit=1)
        if len(parts) < 2:
            return None
        token = parts[0].strip()
    return token if _AGY_MODEL_TOKEN_RE.match(token) else None


def _split_agy_effort_suffix(cli_id: str) -> tuple[str, str | None]:
    for level in _AGY_EFFORT_ORDER:
        suffix = f"-{level}"
        if cli_id.endswith(suffix) and len(cli_id) > len(suffix):
            return cli_id[: -len(suffix)], level
    return cli_id, None


# meiva:note
# Maps external CLI selectors (`[1m]` and Antigravity `-<effort>`) back onto an existing catalog stem.
# meiva:link base/agents/docs/harness/harness-models.md
def resolve_harness_catalog_model(raw_model: str, known_ids: Container[str]) -> str:
    if raw_model in known_ids:
        return raw_model
    candidate = _strip_one_million_suffix(raw_model)
    if candidate in known_ids:
        return candidate
    stem, matched_effort = _split_agy_effort_suffix(candidate)
    if matched_effort is not None and stem in known_ids:
        return stem
    return raw_model


def _build_agy_model(base_id: str, raw_efforts: set[str]) -> ProbedHarnessModel:
    if not raw_efforts:
        return ProbedHarnessModel(id=base_id)
    ordered = tuple(sorted(raw_efforts, key=lambda item: _AGY_EFFORT_RANK[item]))
    default_effort = _choose_effort_default(
        ordered, None, fallbacks=("high", "medium", ordered[-1])
    )
    return ProbedHarnessModel(
        id=base_id,
        reasoning_effort_options=ordered,
        reasoning_effort_default=default_effort,
    )


def parse_antigravity_models(stdout: str) -> CatalogProbeResult:
    order: list[str] = []
    efforts_by_base: dict[str, set[str]] = {}
    for raw_line in stdout.splitlines():
        cli_id = _extract_agy_cli_id(raw_line)
        if cli_id is None:
            continue
        base_id, matched_effort = _split_agy_effort_suffix(cli_id)
        if base_id not in efforts_by_base:
            order.append(base_id)
            efforts_by_base[base_id] = set()
        if matched_effort is not None:
            efforts_by_base[base_id].add(matched_effort)

    if not order:
        return CatalogProbeResult(error="agy models listed no models")
    models = tuple(_build_agy_model(base_id, efforts_by_base[base_id]) for base_id in order)
    return CatalogProbeResult(models=models)


def _select_probed_model(
    harness: Harness,
    models: Sequence[ProbedHarnessModel],
    requested_model: object,
) -> ProbedHarnessModel:
    if not models:
        raise InvalidModelConfig(f"harness {harness.value!r} reported no available models")
    if requested_model is None or requested_model == "":
        raise InvalidModelConfig(
            f"no model configured for harness {harness.value!r} — pass config.llm_model in the spawn request"
        )
    if not isinstance(requested_model, str):
        raise InvalidModelConfig("llm_model must be a string")
    selected = next((item for item in models if item.id == requested_model), None)
    if selected is None:
        available = ", ".join(item.id for item in models)
        raise InvalidModelConfig(
            f"model {requested_model!r} is not supported by harness {harness.value!r} "
            f"(available: {available})"
        )
    return selected


def _select_probed_effort(
    harness: Harness, selected: ProbedHarnessModel, requested_effort: object
) -> str | None:
    if requested_effort is not None and not isinstance(requested_effort, str):
        raise InvalidModelConfig("reasoning_effort must be a string")
    effort_str = (
        requested_effort if isinstance(requested_effort, str) and requested_effort else None
    )
    options = selected.reasoning_effort_options
    if not options:
        if effort_str is not None:
            raise InvalidModelConfig(
                f"unsupported reasoning effort {effort_str!r} for model {selected.id!r} "
                f"on harness {harness.value!r}: model does not accept reasoning_effort"
            )
        return None
    if effort_str is None:
        return _choose_effort_default(options, selected.reasoning_effort_default)
    if effort_str not in options:
        supported = ", ".join(options)
        raise InvalidModelConfig(
            f"unsupported reasoning effort {effort_str!r} for model {selected.id!r} "
            f"on harness {harness.value!r} (supported: {supported})"
        )
    return effort_str


def resolve_probed_harness_selection(
    harness: Harness,
    models: Sequence[ProbedHarnessModel],
    *,
    requested_model: object = None,
    requested_effort: object = None,
) -> tuple[str, str | None]:
    selected = _select_probed_model(harness, models, requested_model)
    resolved_effort = _select_probed_effort(harness, selected, requested_effort)
    return (selected.id, resolved_effort)

# meiva:note
# LLM content stripping (trace v2): pure OTLP span/event attribute filter.
#
# Span attributes that carry LLM content — the 99.89% of the 2026-08-05 mirror
# blowup (31 GB/h with MEIVA_TRACE_ENABLED=true). Chain metadata lives in the
# `traceloop.association.properties.*` / `gen_ai.task.*` / `langgraph.*`
# attributes and never appears here; these keys are pure payload.
#
# Blacklist (exact keys) + prefix rules (gen_ai.input.* / gen_ai.output.*):
# the openllmetry instrumentors emit prompts/completions under gen_ai.* per
# the GenAI semantic convention; traceloop's wrapper mirrors them as
# traceloop.entity.*.

from __future__ import annotations

from base.host.boundary.json_values import is_object_dict, is_object_list

__all__ = ["MAX_ATTR_STRING_CHARS", "strip_content_attributes"]

_CONTENT_ATTR_KEYS: frozenset[str] = frozenset(
    {
        "gen_ai.task.input",
        "gen_ai.task.output",
        "gen_ai.system_instructions",
        "gen_ai.tool_calls",
        "traceloop.entity.input",
        "traceloop.entity.output",
        "messages",
        "system_instructions",
        "inputs",
        "outputs",
    }
)
_CONTENT_ATTR_PREFIXES: tuple[str, ...] = (
    "gen_ai.input.",
    "gen_ai.output.",
)

# meiva:note
# Defensive size guard: a metadata attribute is tiny (names, ids, paths,
# durations); anything bigger on a single string value is treated as content
# even if an instrumentor invents a new key — bounds a future leak to a few
# KB per span. 16 KB is above the largest real metadata (a long langgraph
# path array) and far below the smallest real content (a prompt).
MAX_ATTR_STRING_CHARS = 16 * 1024


# meiva:note
# True when an attribute key carries LLM content and must not reach disk.
def _is_content_attribute(key: str) -> bool:
    if key in _CONTENT_ATTR_KEYS:
        return True
    return any(key.startswith(p) for p in _CONTENT_ATTR_PREFIXES)


# meiva:note
# Decoded OTLP is checked as it is read: a value of another shape raises, since a skipped
# attribute could leak content.
def _as_object(value: object) -> dict[str, object]:
    if not is_object_dict(value):
        raise TypeError(f"OTLP export holds {type(value).__name__} where an object belongs")
    return value


def _objects_at(parent: dict[str, object], key: str) -> list[dict[str, object]]:
    items = parent.get(key, [])
    if not is_object_list(items):
        raise TypeError(f"OTLP export holds {type(items).__name__} at {key!r} where a list belongs")
    return [_as_object(item) for item in items]


# meiva:note
# Approximate serialized size of one OTLP AnyValue dict (chars of the dominant string payload).
# Used by the size guard; a content-free attribute is a few hundred chars at most.
def _attribute_value_size(value: dict[str, object]) -> int:
    for field in ("stringValue", "bytesValue", "intValue", "doubleValue", "boolValue"):
        if field in value:
            return len(str(value[field]))
    if "arrayValue" in value:
        return sum(
            _attribute_value_size(v) for v in _objects_at(_as_object(value["arrayValue"]), "values")
        )
    if "kvlistValue" in value:
        return sum(
            _attribute_value_size(_as_object(kv.get("value", {}))) + len(str(kv.get("key", "")))
            for kv in _objects_at(_as_object(value["kvlistValue"]), "values")
        )
    return 0


# meiva:note
# Keep on a span or event only the attributes that may reach disk (no content, within the guard).
def _strip_attributes(owner: dict[str, object]) -> None:
    kept = [
        kv
        for kv in _objects_at(owner, "attributes")
        if not _is_content_attribute(str(kv.get("key", "")))
        and _attribute_value_size(_as_object(kv.get("value", {}))) <= MAX_ATTR_STRING_CHARS
    ]
    if kept:
        owner["attributes"] = kept
    else:
        owner.pop("attributes", None)


# meiva:note
# Remove LLM-content attributes from a decoded OTLP export request, in place.
#
# Walks resourceSpans -> scopeSpans -> spans (attributes + events) and drops every attribute
# whose key is a known content key (or content prefix), plus any attribute whose string payload
# exceeds `MAX_ATTR_STRING_CHARS` (the size guard — metadata is never large). Span names,
# timestamps, status, ids and `traceloop.*` / `gen_ai.task.*` / `langgraph.*` metadata survive.
def strip_content_attributes(otlp: dict[str, object]) -> None:
    for rs in _objects_at(otlp, "resourceSpans"):
        for ss in _objects_at(rs, "scopeSpans"):
            for span in _objects_at(ss, "spans"):
                _strip_attributes(span)
                for ev in _objects_at(span, "events"):
                    _strip_attributes(ev)

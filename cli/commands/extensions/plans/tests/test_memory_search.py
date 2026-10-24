# meiva:note
# What `meiva memory search` asks and prints, decided from values (`plans/memory_search.py`).
#
# The shell that dials the gateway is proved against a real listener in `test_memory_search.py`;
# this file holds the request body and every rendering rule, including the answers the gateway
# must not be trusted to shape correctly.

from __future__ import annotations

import json

import pytest
from pydantic import JsonValue

from cli.commands.extensions.plans.memory_search import render_results, search_body


def test_the_request_body_names_the_query_the_limit_and_a_human_purpose() -> None:
    assert search_body("alpha", 7) == {"query": "alpha", "k": 7, "purpose": "search"}


def test_the_table_aligns_path_and_tags_to_the_widest_cell() -> None:
    results: list[JsonValue] = [
        {
            "path": "projects/alpha.md",
            "tags": ["project", "active"],
            "description": "Alpha context",
            "score": 0.91,
            "chunk": "Alpha is the active project.",
        },
        {"path": "notes/empty.md", "tags": [], "description": "", "score": 0.5, "chunk": ""},
    ]

    assert render_results({"results": results}, json_output=False).splitlines() == [
        "score  path               tags             description",
        "0.91   projects/alpha.md  project, active  Alpha context",
        "0.50   notes/empty.md                      ",
    ]


def test_the_table_renders_null_metadata_as_empty_text() -> None:
    results: list[JsonValue] = [
        {"path": "relative/note.md", "tags": None, "description": None, "score": 0.25, "chunk": "x"}
    ]

    assert render_results({"results": results}, json_output=False).splitlines() == [
        "score  path              tags  description",
        "0.25   relative/note.md        ",
    ]


def test_an_answer_with_no_results_renders_the_header_alone() -> None:
    assert render_results({"results": []}, json_output=False) == ("score  path  tags  description")


def test_json_output_is_the_results_list_untouched_nulls_included() -> None:
    results: list[JsonValue] = [
        {"path": "relative/note.md", "tags": None, "description": None, "score": 0.8, "chunk": "x"},
        {"path": "relative/empty.md", "tags": [], "description": "", "score": 0.1, "chunk": "y"},
    ]

    assert (
        json.loads(render_results({"paths": [], "results": results}, json_output=True)) == results
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (["not", "an", "object"], "response must be an object"),
        ({"results": "nope"}, "results must be a list"),
    ],
)
def test_an_answer_that_is_not_the_documented_shape_is_refused(
    payload: object, reason: str
) -> None:
    with pytest.raises(TypeError, match=reason):
        render_results(payload, json_output=False)


def test_a_result_row_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(TypeError, match="must contain objects"):
        render_results({"results": ["alpha.md"]}, json_output=False)

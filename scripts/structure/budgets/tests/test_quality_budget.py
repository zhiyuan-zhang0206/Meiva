from __future__ import annotations

from scripts.structure.budgets import quality_budget as quality


# meiva:note
# The line names the function, the budget and the repair; the reason the ceiling exists and the
# document that states it follow, so the author knows what the number protects rather than only
# where the line is. Asserted on fragments, not sentences.
def test_a_violation_names_what_the_ceiling_protects_and_where_the_rule_is_written() -> None:
    lines = quality.quality_errors({"complexity": {"a/b.py::f": 15}, "nesting": {"a/b.py::g": 6}})

    complexity, nesting = lines
    assert complexity.startswith("a/b.py::f: complexity 15: over the 14 ceiling — ")
    assert "cannot be checked by reading it" in complexity
    assert "whoever splits it owns that split" in complexity
    assert nesting.startswith("a/b.py::g: nesting 6: over the 5 ceiling — ")
    assert "every level re-enters a scope" in nesting
    assert all("docs/conventions/python-conventions.md" in line for line in lines)


def test_a_function_inside_the_ceilings_is_silent() -> None:
    assert (
        quality.quality_errors({"complexity": {"a/b.py::f": 14}, "nesting": {"a/b.py::g": 5}}) == []
    )

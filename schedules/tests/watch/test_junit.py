# meiva:note
# What a run's JUnit artifact names, and which artifact belongs to which job: the names-only half of
# task #6487, kept apart from the card decisions it feeds.
# meiva:link schedules/README.md

from __future__ import annotations

from schedules.plans.watch.junit import (
    artifact_name_for_job,
    failing_case_ids,
    matching_artifact,
)


# meiva:note
# The artifact a job's JUnit lands in, derived from the job name rather than kept in a table: the
# shard a job runs is written `2/4` in the job and `2` in the artifact, and every other difference
# is punctuation. A name this cannot reproduce falls back to the unnamed failure at the call site.
def test_the_artifact_name_follows_the_job_name() -> None:
    assert artifact_name_for_job("e2e shard (2/4)") == "test-results-e2e-shard-2"
    assert artifact_name_for_job("backend static 4/5") == "test-results-backend-static-4"
    assert artifact_name_for_job("backend shard 6/8") == "test-results-backend-shard-6"
    assert artifact_name_for_job("data plane (macOS)") == "test-results-data-plane-macos"


# meiva:note
# The exact name wins, and otherwise the longest prefix does: a run publishes `test-results-e2e`
# and `test-results-e2e-shard-2` for different jobs, and answering with the shorter one would name
# a case from another shard. Nothing matching is the honest answer, not the nearest name.
def test_the_artifact_match_prefers_the_exact_name_then_the_longest() -> None:
    names = ("test-results-e2e", "test-results-e2e-shard-2", "test-results-backend-shard-6")
    assert matching_artifact("e2e shard (2/4)", names) == "test-results-e2e-shard-2"
    assert matching_artifact("backend shard 6/8", names) == "test-results-backend-shard-6"
    assert matching_artifact("e2e", names) == "test-results-e2e"
    assert matching_artifact("npm audit", names) is None


# meiva:note
# A JUnit document names a case `classname`/`name`, and the watch names it `file::name`: the file
# attribute is what makes the id identical to the one the log produces, so one test keeps one
# fingerprint whichever route read it. `error` counts with `failure` -- a case that blew up is a red
# nobody can retire by passing -- and a skipped case is not a red at all.
def test_junit_names_the_cases_that_failed_or_errored() -> None:
    document = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.e2e.fakes.test_a" name="test_one" file="tests/e2e/fakes/test_a.py" />'
        '<testcase classname="tests.e2e.fakes.test_a" name="test_two" file="tests/e2e/fakes/test_a.py">'
        "<failure>boom</failure></testcase>"
        '<testcase classname="tests.e2e.fakes.test_b" name="test_three">'
        "<error>kaboom</error></testcase>"
        '<testcase classname="tests.e2e.fakes.test_a" name="test_four" file="tests/e2e/fakes/test_a.py">'
        "<skipped /></testcase>"
        "</testsuite></testsuites>"
    )

    assert failing_case_ids(document) == (
        "tests/e2e/fakes/test_a.py::test_two",
        "tests.e2e.fakes.test_b::test_three",
    )


def test_an_unreadable_junit_document_names_nothing() -> None:
    assert failing_case_ids("<testsuites><testsuite>") == ()
    assert failing_case_ids("") == ()

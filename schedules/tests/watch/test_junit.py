# meiva:note
# What a run's JUnit artifact names, and which artifact belongs to which job: the names-only half of
# task #6487, kept apart from the card decisions it feeds.
# meiva:link schedules/README.md

from __future__ import annotations

from schedules.plans.watch.junit import (
    artifact_candidates,
    artifact_name_for_job,
    failing_case_ids,
    match_artifact,
    matching_artifact,
    shard_number,
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


# meiva:note
# The tier that was missing on 2026-11-08: a matrix job is named after its lane and population
# (`e2e full matrix (3/4)`) while it uploads after its shard (`test-results-e2e-shard-3`), so the
# derived name reaches nothing. The shard number in the job name closes that gap without a table,
# and the log bundle carrying the same number must not win the suffix match.
def test_a_matrix_job_finds_its_artifact_by_the_shard_it_declares() -> None:
    names = (
        "e2e-logs-matrix-3",
        "test-results-e2e-shard-1",
        "test-results-e2e-shard-2",
        "test-results-e2e-shard-3",
        "test-results-e2e-shard-4",
    )

    match = match_artifact("e2e full matrix (3/4)", names)

    assert match.name == "test-results-e2e-shard-3"
    assert match.how == "shard"
    assert match.candidates == ()
    assert match.note == (
        "test-results-e2e-shard-3 (matched by shard: the job name gives no upload name)"
    )


def test_the_shard_number_is_read_from_the_job_name() -> None:
    assert shard_number("e2e full matrix (3/4)") == "3"
    assert shard_number("Backend full suite (8/8)") == "8"
    assert shard_number("backend shard 6/8") == "6"
    assert shard_number("data plane (macOS)") is None


# meiva:note
# Two artifacts ending in the same shard number answer nothing: picking one would put a real test id
# on another job's red. The near misses go on the card instead, filtered to the names that share a
# word with the job so a backend card does not advertise e2e bundles.
def test_an_ambiguous_shard_names_no_artifact_and_offers_the_candidates() -> None:
    names = ("test-results-e2e-shard-3", "test-results-e2e-matrix-3", "test-results-frontend")

    match = match_artifact("e2e full matrix (3/4)", names)

    assert match.name is None
    assert match.how == "none"
    assert match.candidates == ("test-results-e2e-matrix-3", "test-results-e2e-shard-3")


# meiva:note
# The bug the first cut of the shard tier had, caught by replaying the real run: `Backend full suite
# (4/8)` matched `test-results-e2e-shard-4`, because the run published no backend artifact and the
# only thing the two names shared was the shard number. A backend card naming an e2e case is the
# misattribution the whole three-tier rule exists to prevent, so the shard tier also demands a word
# in common with the job.
def test_a_lane_never_takes_another_lanes_shard() -> None:
    names = ("test-results-e2e-shard-4", "test-results-e2e-shard-3")

    match = match_artifact("Backend full suite (4/8)", names)

    assert match.name is None
    assert match.how == "none"
    assert match.candidates == ()


def test_a_job_with_no_artifact_gets_no_candidates_from_another_lane() -> None:
    names = ("test-results-e2e-shard-1", "test-results-e2e-shard-2")

    assert artifact_candidates("Backend full suite (8/8)", names) == ()
    assert match_artifact("Backend full suite (8/8)", names).name is None

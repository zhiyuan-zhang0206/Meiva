from __future__ import annotations

import re

import pytest

from base.events.contract import TIER_BY_EVENT, EventGroupBy
from gateway.events.loki_query import LokiFilters, count_query, log_query, tier_filter

_STREAM = 'service_name="unknown_service"'
_NOT_PROBE = 'agent_id!="watchdog"'


def test_a_bare_read_selects_the_event_stream_without_the_write_probe() -> None:
    assert log_query(LokiFilters()) == f"{{{_STREAM}, {_NOT_PROBE}}}"


def test_an_agent_and_event_names_are_index_labels_in_the_selector() -> None:
    query = log_query(LokiFilters(agent_id=228, event_names=("spawn", "exec")))
    assert query == f'{{{_STREAM}, agent_id="228", event_name=~"^(?:spawn|exec)$"}}'
    assert log_query(LokiFilters(event_names=("spawn",))) == (
        f'{{{_STREAM}, {_NOT_PROBE}, event_name="spawn"}}'
    )


def test_service_only_selects_the_events_that_belong_to_no_agent() -> None:
    assert log_query(LokiFilters(service_only=True)) == f'{{{_STREAM}, agent_id=""}}'


def test_level_category_and_trace_are_read_from_structured_metadata() -> None:
    query = log_query(
        LokiFilters(level="WARNING", categories=("telemetry", "log"), trace_id="ab12")
    )
    assert query == (
        f'{{{_STREAM}, {_NOT_PROBE}}} | category=~"^(?:telemetry|log)$" | level="warning" '
        '| trace_id="ab12"'
    )


def test_a_minimum_level_lists_the_levels_from_it_up() -> None:
    assert log_query(LokiFilters(level_min="error")) == (
        f'{{{_STREAM}, {_NOT_PROBE}}} | level=~"^(?:error|critical)$"'
    )
    assert 'level=~"^(?:debug|info|warning|error|critical)$"' in (
        log_query(LokiFilters(level_min="debug")) or ""
    )


def test_the_machine_is_read_from_the_body_because_older_lines_carry_no_label() -> None:
    assert log_query(LokiFilters(machine="company-mini")) == (
        f'{{{_STREAM}, {_NOT_PROBE}}} | json event_machine="machine" | event_machine="company-mini"'
    )


def test_grep_is_a_case_insensitive_literal_line_filter() -> None:
    assert log_query(LokiFilters(grep="a.b (x)")) == (
        f'{{{_STREAM}, {_NOT_PROBE}}} |~ "(?i)a\\\\.b \\\\(x\\\\)"'
    )


def test_a_value_cannot_break_out_of_its_string() -> None:
    query = log_query(LokiFilters(trace_id='x" | drop level', grep='"\n'))
    assert query is not None
    assert '| trace_id="x\\" | drop level"' in query
    assert "\n" not in query


def test_a_name_is_escaped_for_the_regex() -> None:
    query = log_query(LokiFilters(event_names=("a.b", "c|d")))
    assert query is not None
    assert 'event_name=~"^(?:a\\\\.b|c\\\\|d)$"' in query


def _regex_of(clause: str, label: str) -> set[str]:
    match = re.search(rf'{label}[!=]~"\^\(\?:(.*?)\)\$"', clause)
    assert match is not None
    return set(match.group(1).split("|"))


def test_a_regex_is_anchored_because_a_bare_alternation_misses_prefix_names() -> None:
    clause = tier_filter(("noise",))
    assert clause is not None
    assert 'event_name=~"^(?:' in clause
    assert 'level!~"^(?:' in clause


def test_the_anomaly_tier_is_a_warning_level_or_a_declared_anomaly_name() -> None:
    clause = tier_filter(("anomaly",))
    assert clause is not None
    assert clause.startswith('(level=~"^(?:warning|error|critical)$" or event_name=~')
    declared = {name for name, tier in TIER_BY_EVENT.items() if tier == "anomaly"}
    assert _regex_of(clause, "event_name") == declared


def test_the_noise_tier_is_a_declared_noise_name_below_warning() -> None:
    clause = tier_filter(("noise",))
    assert clause is not None
    assert clause.startswith('(level!~"^(?:warning|error|critical)$" and event_name=~')
    assert _regex_of(clause, "event_name") == {
        name for name, tier in TIER_BY_EVENT.items() if tier == "noise"
    }


def test_the_observation_tier_is_every_other_name_below_warning() -> None:
    clause = tier_filter(("observation",))
    assert clause is not None
    assert clause.startswith('(level!~"^(?:warning|error|critical)$" and event_name!~')
    assert _regex_of(clause, "event_name") == {
        name for name, tier in TIER_BY_EVENT.items() if tier != "observation"
    }


def test_tiers_are_ored_and_business_matches_no_telemetry_row() -> None:
    both = tier_filter(("anomaly", "noise", "anomaly"))
    assert both is not None
    assert both.count(" or (level!~") == 1
    assert tier_filter(("business", "noise")) == tier_filter(("noise",))
    assert tier_filter(("business",)) is None


def test_a_read_that_no_row_can_match_has_no_query() -> None:
    assert log_query(LokiFilters(tiers=("business",))) is None
    assert count_query(LokiFilters(tiers=("business",)), by=None, range_ms=1000) is None


def test_the_tier_clause_follows_the_other_stages() -> None:
    query = log_query(LokiFilters(categories=("log",), tiers=("noise",), machine="m1"))
    assert query is not None
    assert query.index("| category=") < query.index("| (level!~") < query.index("| json")


def test_a_total_counts_over_the_window_in_milliseconds() -> None:
    assert count_query(LokiFilters(agent_id=7), by=None, range_ms=86_400_000) == (
        f'sum(count_over_time({{{_STREAM}, agent_id="7"}} [86400000ms]))'
    )


@pytest.mark.parametrize("by", ["event_name", "agent_id"])
def test_a_group_by_an_index_label_needs_no_stage(by: EventGroupBy) -> None:
    query = count_query(LokiFilters(), by=by, range_ms=1000)
    assert query == f"sum by ({by}) (count_over_time({{{_STREAM}, {_NOT_PROBE}}} [1000ms]))"


@pytest.mark.parametrize("by", ["level", "category"])
def test_a_group_by_structured_metadata_names_the_label_in_a_stage(by: EventGroupBy) -> None:
    query = count_query(LokiFilters(), by=by, range_ms=1000)
    assert query == (
        f'sum by ({by}) (count_over_time({{{_STREAM}, {_NOT_PROBE}}} | {by}=~".*" [1000ms]))'
    )


def test_a_group_by_machine_reads_the_body_once() -> None:
    assert count_query(LokiFilters(), by="machine", range_ms=1000) == (
        f"sum by (event_machine) (count_over_time({{{_STREAM}, {_NOT_PROBE}}} "
        '| json event_machine="machine" [1000ms]))'
    )
    filtered = count_query(LokiFilters(machine="m1"), by="machine", range_ms=1000)
    assert filtered is not None
    assert filtered.count("| json") == 1

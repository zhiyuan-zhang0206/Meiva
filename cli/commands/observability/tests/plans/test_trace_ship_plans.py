# meiva:note
# Where `meiva trace ship` replays the mirror and which files, offsets and ids it works on,
# decided from values alone.

from __future__ import annotations

from base64 import b64decode
from datetime import date

import pytest

from cli.commands.observability.plans.trace_ship.files import (
    FileCursor,
    ShipTotals,
    file_report_line,
    live_watermarks,
    plan_file_cursor,
    repaired_span_id,
    ship_summary_lines,
    ship_window,
)
from cli.commands.observability.plans.trace_ship.target import (
    ShipTarget,
    TraceShipError,
    plan_ship_target,
)
from cli.commands.observability.url_split import split_url

# meiva:note
# The telemetry token of the unit under test: a fixture value, not a credential.
TOKEN = "unit-token"  # noqa: S105

GATEWAY = frozenset({"gateway"})
SINGLE_BOX = frozenset({"gateway", "agent-runner"})
RUNNER = frozenset({"agent-runner"})


def target(
    roles: frozenset[str],
    *,
    enabled: bool = True,
    gateway: str = "http://10.0.0.10:4318",
    token: str | None = None,
) -> ShipTarget:
    return plan_ship_target(
        otlp_enabled=enabled,
        roles=roles,
        tempo_endpoint="http://tempo.test:14318/",
        gateway_endpoint=gateway,
        gateway=split_url(gateway),
        token=token,
    )


@pytest.mark.parametrize("roles", [GATEWAY, SINGLE_BOX])
def test_a_gateway_replays_straight_to_its_loopback_tempo_without_auth(
    roles: frozenset[str],
) -> None:
    assert target(roles) == ShipTarget(
        endpoint="http://tempo.test:14318/v1/traces", headers={}, label="tempo"
    )


def test_a_pure_runner_replays_to_the_gateway_relay_with_its_telemetry_token() -> None:
    assert target(RUNNER, token=TOKEN) == ShipTarget(
        endpoint="http://10.0.0.10:4318/v1/traces",
        headers={"Authorization": f"Bearer {TOKEN}"},
        label="gateway OTLP relay",
    )


@pytest.mark.parametrize("roles", [GATEWAY, RUNNER])
def test_the_otlp_kill_switch_stops_shipping_for_every_role(roles: frozenset[str]) -> None:
    with pytest.raises(TraceShipError, match="MEIVA_TELEMETRY_OTLP_ENABLED"):
        target(roles, enabled=False)


@pytest.mark.parametrize("token", [None, ""])
def test_a_runner_without_a_capability_token_cannot_ship(token: str | None) -> None:
    with pytest.raises(TraceShipError, match="holds no capability"):
        target(RUNNER, token=token)


def test_a_runner_whose_gateway_has_not_published_an_ingress_cannot_ship() -> None:
    with pytest.raises(TraceShipError, match="gateway bootstrap"):
        target(RUNNER, gateway="", token=TOKEN)


def test_no_window_means_incremental_and_every_day_is_inside() -> None:
    window = ship_window(None, None)

    assert not window.windowed
    assert window.scope == "incremental"
    assert window.contains(date(1999, 1, 1))
    assert window.contains(date(2999, 12, 31))


def test_a_window_is_inclusive_on_both_days() -> None:
    window = ship_window("2026-06-15", "2026-06-17")

    assert window.windowed
    assert window.scope == "window 2026-06-15..2026-06-17"
    assert not window.contains(date(2026, 6, 14))
    assert window.contains(date(2026, 6, 15))
    assert window.contains(date(2026, 6, 17))
    assert not window.contains(date(2026, 6, 18))


def test_a_half_open_window_names_the_open_end() -> None:
    assert ship_window("2026-06-15", None).scope == "window 2026-06-15..+inf"
    assert ship_window(None, "2026-06-17").scope == "window -inf..2026-06-17"


def test_a_malformed_day_is_refused() -> None:
    with pytest.raises(ValueError, match="does not match"):
        ship_window("15/06/2026", None)


ROTATED = "spans-2026-08-27T03-29-10.942-size.jsonl"


def test_an_incremental_ship_starts_a_plain_file_at_its_watermark() -> None:
    cursor = plan_file_cursor(ROTATED, windowed=False, marks={ROTATED: 40}, size=100)

    assert cursor == FileCursor(key=ROTATED, start=40, gzipped=False, shipped_out=False)


def test_a_plain_file_shipped_to_its_end_has_nothing_left() -> None:
    assert plan_file_cursor(ROTATED, windowed=False, marks={ROTATED: 100}, size=100).shipped_out


def test_a_windowed_ship_ignores_the_watermark_and_starts_at_zero() -> None:
    cursor = plan_file_cursor(ROTATED, windowed=True, marks={ROTATED: 99999}, size=100)

    assert cursor.start == 0
    assert not cursor.shipped_out


def test_a_gzipped_segment_is_keyed_by_its_base_name_so_compression_strands_nothing() -> None:
    cursor = plan_file_cursor(f"{ROTATED}.gz", windowed=False, marks={ROTATED: 25}, size=0)

    assert cursor == FileCursor(key=ROTATED, start=25, gzipped=True, shipped_out=False)


def test_a_gzipped_segment_is_always_opened_because_its_decoded_size_is_unknown() -> None:
    assert not plan_file_cursor(
        f"{ROTATED}.gz", windowed=False, marks={ROTATED: 10**9}, size=0
    ).shipped_out


def test_the_watermark_sheds_files_the_retention_pass_pruned() -> None:
    marks = {"spans.jsonl": 2, ROTATED: 5, "spans-gone.jsonl": 7}

    kept = live_watermarks(marks, present=frozenset({"spans.jsonl", f"{ROTATED}.gz"}))

    assert kept == {"spans.jsonl": 2, ROTATED: 5}


@pytest.mark.parametrize(
    ("hex_id", "length"), [("0123456789abcdef", 8), ("0123456789abcdef" * 2, 16)]
)
def test_an_id_the_json_parser_read_as_base64_is_repaired_to_the_raw_bytes_of_its_hex(
    hex_id: str, length: int
) -> None:
    parsed = b64decode(hex_id)

    assert len(parsed) != length
    assert repaired_span_id(parsed, length) == bytes.fromhex(hex_id)


def test_an_id_of_the_expected_length_is_left_alone() -> None:
    assert repaired_span_id(bytes(8), 8) is None


def test_an_empty_id_is_left_alone() -> None:
    assert repaired_span_id(b"", 8) is None


def test_an_id_that_cannot_be_repaired_to_the_expected_length_is_left_alone() -> None:
    assert repaired_span_id(b"abc", 8) is None


def test_the_reports_name_the_file_the_totals_and_the_target() -> None:
    assert (
        file_report_line("spans.jsonl", batches=2, spans=5) == "  spans.jsonl: 2 batches, 5 spans"
    )
    assert ship_summary_lines(
        dry_run=False,
        scope="incremental",
        totals=ShipTotals(files=1, batches=2, spans=5),
        target_line="target: tempo -> http://tempo.test:14318/v1/traces",
    ) == (
        "shipped (incremental): 1 files, 2 batches, 5 spans",
        "target: tempo -> http://tempo.test:14318/v1/traces",
    )
    assert (
        ship_summary_lines(
            dry_run=True,
            scope="window 2026-06-15..+inf",
            totals=ShipTotals(0, 0, 0),
            target_line="target: (dry-run — role-correct target not resolved)",
        )[0]
        == "would ship (window 2026-06-15..+inf): 0 files, 0 batches, 0 spans"
    )

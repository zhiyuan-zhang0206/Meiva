# meiva:note
# What `meiva status` and `meiva start` print about service probes.
#
# The wording is decided from values (`probe_report`) and asserted on its return value. The
# shell (`probe`) is run against real listeners: a loopback HTTP server that answers the
# unit's home, another unit's home, or nothing at all.

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

import pytest

from base.daemon.health import probe_home
from base.host.net.tests.local_http import closed_port_url, serve_json
from base.paths import meiva_home
from cli.commands.probe import (
    ReadinessWait,
    print_non_critical_unready_services,
    print_service_row,
    print_unready_services,
)
from cli.commands.probe_report import (
    ServiceRow,
    UnreadyWait,
    non_critical_unready_text,
    service_row_line,
    unready_text,
)
from services.supervision.roster.service_spec import GATEWAY, ServiceSpec

_ROW = ServiceRow(
    name="meiva-ops",
    root_running=True,
    probe_alive=True,
    probe_label="identity",
    probe_detail="",
    skip_reason=None,
)


def _row(**overrides: Any) -> ServiceRow:
    return replace(_ROW, **overrides)


@pytest.mark.parametrize(
    ("alive", "mark"), [(True, "✓"), (False, "✗"), (None, "·")], ids=["up", "down", "unknown"]
)
def test_the_probe_mark_is_positive_negative_or_unknown(alive: bool | None, mark: str) -> None:
    assert service_row_line(_row(probe_alive=alive), 12) == f"meiva-ops     ✓     {mark} (identity)"


def test_a_session_root_does_not_run_reads_as_a_cross() -> None:
    assert service_row_line(_row(root_running=False), 12).split()[1] == "✗"


def test_a_failing_probe_names_its_fact_on_the_same_line() -> None:
    line = service_row_line(
        _row(probe_alive=False, probe_detail="home='/home/meiva/.meiva' != '/u/.meiva'"), 12
    )

    assert line.endswith("   -- home='/home/meiva/.meiva' != '/u/.meiva'")


def test_a_gate_reason_outranks_the_probe_detail() -> None:
    line = service_row_line(_row(probe_detail="down", skip_reason="no display (headless)"), 12)

    assert line.endswith("   -- skipped: no display (headless)")
    assert "down" not in line


def test_a_wait_that_found_every_session_gone_does_not_blame_the_bound() -> None:
    wait = UnreadyWait(names=("meiva-ops", "meiva-gate"), elapsed_s=1.2, sessions_gone=True)

    text = unready_text(wait, 180.0)

    assert (
        "2 service(s) not ready after 1.2s — their sessions are gone: meiva-ops, meiva-gate" in text
    )
    assert "180s readiness bound was not spent" in text
    assert "never became ready" not in text


def test_a_wait_that_spent_the_bound_names_it() -> None:
    wait = UnreadyWait(names=("meiva-ops",), elapsed_s=180.4, sessions_gone=False)

    text = unready_text(wait, 180.0)

    assert "1 service(s) never became ready within 180s (180.4s elapsed): meiva-ops" in text
    assert "sessions are gone" not in text


def test_the_non_critical_tier_is_named_and_cannot_fail_the_start() -> None:
    text = non_critical_unready_text(("meiva-labeler", "meiva-watcher"))

    assert "2 non-critical service(s) currently unavailable: meiva-labeler, meiva-watcher" in text
    assert "do not fail this start" in text
    assert "service_start_unready event was emitted" in text


def _spec(session: str, *, url: str | None) -> ServiceSpec:
    return ServiceSpec(
        session=session,
        cmd="unused",
        capabilities=GATEWAY,
        requires_db=False,
        identity_probe=None
        if url is None
        else partial(probe_home, url, expected_home=meiva_home(), timeout_s=2.0),
    )


def test_a_row_for_a_service_answering_this_units_home_is_live(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serve_json({"home": str(meiva_home())}) as served:
        print_service_row(
            _spec("ops", url=served.url), 12, None, root_units={"ops": {"state": "running"}}
        )

    assert capsys.readouterr().out == "meiva-ops     ✓     ✓ (identity)\n"


def test_a_row_for_another_units_listener_carries_its_identity_mismatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serve_json({"home": "/home/meiva/.meiva"}) as served:
        print_service_row(
            _spec("ops", url=served.url), 12, None, root_units={"ops": {"state": "running"}}
        )

    row = capsys.readouterr().out
    assert row.startswith("meiva-ops     ✓     ✗ (identity)   -- identity mismatch")
    assert "/home/meiva/.meiva" in row


def test_a_row_for_a_silent_port_reads_down_and_a_stopped_unit_reads_a_cross(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_service_row(
        _spec("ops", url=closed_port_url() + "/healthz"),
        12,
        None,
        root_units={"ops": {"state": "stopped"}},
    )

    row = capsys.readouterr().out
    assert row.startswith("meiva-ops     ✗     ✗ (identity)   -- healthz unreachable")


def test_a_service_without_an_identity_probe_cannot_claim_readiness(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_service_row(_spec("ops", url=None), 12, None, root_units={})

    assert capsys.readouterr().out == (
        "meiva-ops     ✗     · (unavailable)   -- service has no identity-bound readiness probe\n"
    )


def test_a_gated_service_still_reports_its_live_marks(capsys: pytest.CaptureFixture[str]) -> None:
    with serve_json({"home": str(meiva_home())}) as served:
        print_service_row(
            _spec("browser", url=served.url),
            16,
            "no display (headless)",
            root_units={"browser": {"state": "running"}},
        )

    out = capsys.readouterr().out
    assert out == "meiva-browser     ✓     ✓ (identity)   -- skipped: no display (headless)\n"


def test_the_unready_summaries_print_to_stderr_with_composed_session_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ops = _spec("ops", url=None)
    labeler = _spec("labeler", url=None)

    print_unready_services(ReadinessWait((ops,), 3.0, sessions_gone=True), 180.0)
    print_non_critical_unready_services((labeler,))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "their sessions are gone: meiva-ops" in captured.err
    assert "non-critical service(s) currently unavailable: meiva-labeler" in captured.err

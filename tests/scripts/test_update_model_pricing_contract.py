from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.model_registry import update_model_pricing as pricing_updater

_REPO_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# The official page as it stands today (checked 2026-10-29): the V4 Pro column carries no
# footnote marker and the Flash column's identity is the 1017 build.
_CURRENT_TABLE = """
<html><main>
<table>
  <tr><td colspan="3">MODEL</td><td>deepseek-flash (1)</td><td>deepseek-v4-pro</td></tr>
  <tr><td colspan="3">MODEL VERSION</td><td>DeepSeek-V4.1-Flash-1017</td><td>DeepSeek-V4-Pro-0813</td></tr>
  <tr><td rowspan="6">PRICING (2)</td><td rowspan="2">1M INPUT TOKENS (CACHE HIT)</td><td>OFF-PEAK</td><td>$0.003</td><td>$0.022</td></tr>
  <tr><td>PEAK</td><td>$0.006</td><td>$0.044</td></tr>
  <tr><td rowspan="2">1M INPUT TOKENS (CACHE MISS)</td><td>OFF-PEAK</td><td>$0.15</td><td>$0.66</td></tr>
  <tr><td>PEAK</td><td>$0.3</td><td>$1.32</td></tr>
  <tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.6</td><td>$1.98</td></tr>
  <tr><td>PEAK</td><td>$1.2</td><td>$3.96</td></tr>
</table>
<p>Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday (all other hours are off-peak).</p>
<p>(1) Use deepseek-flash as the model name. The legacy names deepseek-v4-flash and deepseek-v4-flash-vision-exp are still accepted, but the corresponding models have been retired, their requests are served by the DeepSeek-V4.1-Flash-1017 model and billed at the Flash price.</p>
<p>(2) Off-peak rates are half of the peak rates.</p>
</main></html>
"""

# meiva:note
# The 2026-09-11 page: the V4 Pro column carried footnote (2), first as a retirement announcement
# and then, the same day, as its withdrawal. The prices and the identity are identical to
# today's, so the annotation is the only thing that separates the two states.
_ANNOTATED_TABLE = _CURRENT_TABLE.replace(
    "<td>deepseek-v4-pro</td>", "<td>deepseek-v4-pro (2)</td>", 1
)


def _reviewed_catalog() -> dict[str, Any]:
    return json.loads((_REPO_ROOT / "base/lm/pricing/pricing_catalog_archive.json").read_text())


# meiva:note
# The archive as it stood until 2026-10-29: V4 Pro billed at Flash rates from 2026-09-14T04:00:00Z.
def _prior_catalog_with_the_retired_band() -> dict[str, Any]:
    catalog = _reviewed_catalog()
    flash_band = deepcopy(catalog["models"]["deepseek-flash"]["periods"][0])
    periods = catalog["models"]["deepseek-v4-pro"]["periods"]
    periods[-1]["effective_until"] = "2026-09-14T04:00:00Z"
    periods.append(
        {
            "effective_from": "2026-09-14T04:00:00Z",
            "effective_until": None,
            "tiers": deepcopy(flash_band["tiers"]),
        }
    )
    return catalog


def _reconcile(catalog: dict[str, Any], table: str, detected_at: str) -> dict[str, Any] | None:
    return pricing_updater.reconcile_deepseek_catalog(
        catalog,
        pricing_updater.parse_deepseek_pricing(table),
        detected_at=detected_at,
    )


def test_reconcile_is_a_noop_when_the_reviewed_catalog_matches() -> None:
    assert _reconcile(_reviewed_catalog(), _CURRENT_TABLE, "2026-10-29T12:34:56Z") is None


def test_reconcile_rejects_an_unexpected_column_roster() -> None:
    with pytest.raises(ValueError, match="must contain exactly"):
        _reconcile(
            _reviewed_catalog(),
            _CURRENT_TABLE.replace("deepseek-flash (1)", "deepseek-v4.1-flash (1)"),
            "2026-10-29T12:34:56Z",
        )


def test_reconcile_appends_a_new_effective_period_without_rewriting_history() -> None:
    catalog = _reviewed_catalog()
    table = _CURRENT_TABLE.replace("<td>$1.2</td>", "<td>$1.28</td>").replace(
        "<td>$0.6</td>", "<td>$0.64</td>"
    )

    updated = _reconcile(catalog, table, "2026-10-29T12:34:56Z")

    assert updated is not None
    for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
        periods = updated["models"][model]["periods"]
        assert periods[-2]["effective_until"] == "2026-10-29T12:34:56Z"
        assert periods[-1]["effective_from"] == "2026-10-29T12:34:56Z"
        assert periods[-1]["tiers"][0]["rates"]["output"] == "0.64"
        assert periods[-1]["tiers"][0]["utc_daily_overrides"][0]["rates"]["output"] == "1.28"
    assert catalog["models"]["deepseek-v4-flash"]["periods"][-1]["effective_until"] is None


def test_reconcile_appends_a_period_when_peak_windows_change() -> None:
    table = _CURRENT_TABLE.replace(
        "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC,",
        "Peak hours are 02:00 - 05:00 and 07:00 - 11:00 UTC,",
    )

    updated = _reconcile(_reviewed_catalog(), table, "2026-10-29T12:34:56Z")

    assert updated is not None
    overrides = updated["models"]["deepseek-v4-flash"]["periods"][-1]["tiers"][0][
        "utc_daily_overrides"
    ]
    assert [(item["start"], item["end"]) for item in overrides] == [
        ("02:00:00", "05:00:00"),
        ("07:00:00", "11:00:00"),
    ]


# meiva:note
# A footnote marker is a claim about the column; the adapter never turns it into a rule.
def test_reconcile_fails_closed_when_a_column_carries_an_unreviewed_footnote() -> None:
    with pytest.raises(ValueError, match="footnote marker '2'"):
        _reconcile(_reviewed_catalog(), _ANNOTATED_TABLE, "2026-10-29T12:34:56Z")


def test_reconcile_fails_closed_when_a_model_identity_changes_without_a_price_change() -> None:
    with pytest.raises(ValueError, match=r"DeepSeek-V4\.1-Flash'"):
        _reconcile(
            _reviewed_catalog(),
            _CURRENT_TABLE.replace("DeepSeek-V4.1-Flash-1017", "DeepSeek-V4.1-Flash", 1),
            "2026-10-29T12:34:56Z",
        )


def test_reconcile_keeps_the_pro_band_open_after_the_withdrawn_retirement() -> None:
    periods = _reviewed_catalog()["models"]["deepseek-v4-pro"]["periods"]

    assert [period["effective_until"] for period in periods] == ["2026-08-16T16:00:00Z", None]
    assert periods[-1]["tiers"][0]["rates"] == {
        "input": "0.66",
        "cache_read": "0.022",
        "output": "1.98",
    }


# meiva:note
# A MODEL label's cited footnote prices the retired Flash ids; a rewrite under the same marker must
# not pass as a match.
def test_reconcile_fails_closed_when_a_cited_footnote_body_changes() -> None:
    with pytest.raises(ValueError, match=r"footnote \(1\).*deepseek-flash"):
        _reconcile(
            _reviewed_catalog(),
            _CURRENT_TABLE.replace("billed at the Flash price", "billed at twice the Flash price"),
            "2026-10-29T12:34:56Z",
        )


# meiva:note
# A cited marker whose text disappeared cannot be reviewed, so it fails instead of matching.
def test_reconcile_fails_closed_when_a_cited_footnote_body_is_missing() -> None:
    stripped = _CURRENT_TABLE.replace(
        "<p>(1) Use deepseek-flash as the model name. The legacy names deepseek-v4-flash and "
        "deepseek-v4-flash-vision-exp are still accepted, but the corresponding models have been "
        "retired, their requests are served by the DeepSeek-V4.1-Flash-1017 model and billed at "
        "the Flash price.</p>",
        "",
    )

    with pytest.raises(ValueError, match="no such footnote body"):
        _reconcile(_reviewed_catalog(), stripped, "2026-10-29T12:34:56Z")


# meiva:note
# Provenance compares normalized text: reflowing a reviewed footnote is not a change.
def test_reconcile_ignores_whitespace_reflow_in_a_cited_footnote() -> None:
    reflowed = _CURRENT_TABLE.replace("their requests are served", "their requests\n   are  served")

    assert _reconcile(_reviewed_catalog(), reflowed, "2026-10-29T12:34:56Z") is None


# meiva:note
# Only the markers a MODEL label cites are compared, so an unrelated footnote may change.
def test_reconcile_compares_only_the_footnotes_a_model_label_cites() -> None:
    uncited = _CURRENT_TABLE.replace(
        "(2) Off-peak rates are half of the peak rates.",
        "(2) Off-peak rates are thirty percent of peak.",
    )

    assert _reconcile(_reviewed_catalog(), uncited, "2026-10-29T12:34:56Z") is None


# meiva:note
# The 2026-09-14 Flash-priced band was never true; it must not read as a match.
def test_reconcile_reports_the_retired_band_as_drift_instead_of_matching() -> None:
    prior = _prior_catalog_with_the_retired_band()

    updated = _reconcile(prior, _CURRENT_TABLE, "2026-10-29T12:00:00Z")

    assert updated is not None
    periods = updated["models"]["deepseek-v4-pro"]["periods"]
    assert [(period["effective_from"], period["effective_until"]) for period in periods] == [
        (None, "2026-08-16T16:00:00Z"),
        ("2026-08-16T16:00:00Z", "2026-09-14T04:00:00Z"),
        ("2026-09-14T04:00:00Z", "2026-10-29T12:00:00Z"),
        ("2026-10-29T12:00:00Z", None),
    ]
    for before, after in zip(prior["models"]["deepseek-v4-pro"]["periods"], periods, strict=False):
        assert after["tiers"] == before["tiers"]
    assert periods[-1]["effective_from"] == "2026-10-29T12:00:00Z"
    assert periods[-1]["tiers"][0]["rates"] == {
        "input": "0.66",
        "cache_read": "0.022",
        "output": "1.98",
    }


def test_workflow_runs_only_trusted_main_code_with_write_permissions() -> None:
    workflow = (_REPO_ROOT / ".github/workflows/update-model-pricing.yml").read_text()

    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "ref: main" in workflow
    assert 'git worktree add -B "$BRANCH" "$CANDIDATE"' in workflow
    assert '[ -L "$ARCHIVE" ]' in workflow
    assert 'ARCHIVE_REAL="$(realpath "$ARCHIVE")"' in workflow
    assert '"$CANDIDATE_REAL"/*' in workflow
    assert (
        'python scripts/model_registry/update_model_pricing.py --catalog "$ARCHIVE" --write'
        in workflow
    )
    assert (
        "python scripts/model_registry/update_model_pricing.py --sync-plugins --write" in workflow
    )
    assert "meiva_builtins/plugins/lm_*/*.py" in workflow
    assert "bot sync → human review of the PR" in workflow
    assert "model-pricing-future-windows.md" in workflow
    assert 'cd "$CANDIDATE"' not in workflow
    assert "git checkout" not in workflow

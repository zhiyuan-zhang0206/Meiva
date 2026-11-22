from __future__ import annotations

import pytest

from cli.parsers import parse_args


def test_metrics_backfill_unpriced_parser_flags() -> None:
    default_args = parse_args(["metrics", "backfill-unpriced"], retained_children=[])
    assert default_args.metrics_cmd == "backfill-unpriced"
    assert default_args.apply is False
    assert default_args.json is False

    dry_args = parse_args(
        ["metrics", "backfill-unpriced", "--dry-run", "--json"], retained_children=[]
    )
    assert dry_args.apply is False
    assert dry_args.json is True

    apply_args = parse_args(["metrics", "backfill-unpriced", "--apply"], retained_children=[])
    assert apply_args.apply is True

    with pytest.raises(SystemExit):
        parse_args(["metrics", "backfill-unpriced", "--dry-run", "--apply"], retained_children=[])

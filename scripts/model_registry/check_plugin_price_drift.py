#!/usr/bin/env python3
# meiva:note
# The commit-time gate wired in `.pre-commit-config.yaml`. Every built-in provider's committed
# effective periods, token tiers, rate windows and provenance must be the archive's; a run that
# compared no priced model fails instead of passing an empty comparison. The flat trio is excluded
# because it summarizes the base tier at the last synchronization instant.
# meiva:link base/lm/pricing/docs/pricing.meiva.okf.md

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.model_registry.plugin_price_sync import lattice_drift

_REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare built-in provider prices with the archive."
    )
    parser.add_argument("--catalog", type=Path, help="archive to compare the declarations with")
    parser.add_argument("--repo-root", type=Path, help="repository holding the built-in providers")
    args = parser.parse_args(argv)
    repo_root = args.repo_root or _REPO_ROOT
    catalog = args.catalog or repo_root / "base/lm/pricing/pricing_catalog_archive.json"

    report = lattice_drift(catalog, repo_root)
    print(
        f"{report.provider_files} provider file(s), "
        f"{report.priced_models} priced model(s), {len(report.drifted)} drifted"
    )
    for label in report.drifted:
        print(f"- {label}", file=sys.stderr)
    if report.priced_models == 0:
        print("no priced model was compared, so this run proves nothing", file=sys.stderr)
        return 1
    return 1 if report.drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())

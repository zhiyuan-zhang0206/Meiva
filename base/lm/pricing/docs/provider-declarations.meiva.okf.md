---
type: doc
title: Provider Price Declarations, the Drift Gate and the Nightly Rewrite
description: The shapes a built-in provider uses to declare prices, and the drift gate and nightly rewrite that read and write them.
tags:
- base
- library
- llm-inference
- billing
---

# Provider Price Declarations and Their Generators

The commit-time gate and the nightly reconciliation both read provider price declarations out of the tree; this node owns their shapes and what each script may write. [`pricing.meiva.okf.md`](pricing.meiva.okf.md) owns the ledger, the selection rules and the sourcing rules.

## Declaration forms

- A model whose whole price is one unbounded tier carrying exactly the flat trio declares those three rates alone — the period `plugin_model_price` materializes from them — and the gate compares it by them.
- The package is the declaration unit: a face may name a literal (`models=MODELS`, `with_fast_variants(PROVIDER, FAST_PRICES)`) another module of the same package assigns, names resolve only inside that package, and a rewrite lands in the file that holds each price.
- Every price-bearing declaration in a provider package participates — chat and decision contributions alike, recognized by the provider contract they import rather than by a class name, because decision calls bill from the same `PriceBook`.

## The gate and the nightly rewrite

- **Commit-time gate.** `scripts/model_registry/check_plugin_price_drift.py` is the commit-time gate (the `plugin-price-lattice` pre-commit hook, and CI's `--all-files` structure run): every built-in declaration's effective periods, token tiers, rate windows and provenance must equal the archive's, compared without the clock so the verdict cannot change on an effective date that the nightly run has not rewritten yet. The flat trio is not compared with the current base: it summarizes one period's base at the last synchronization instant, which the tree does not record, so the gate requires only that those three numbers name a base tier the archive states (any period's, three prices only). A declaration that carries no periods is compared by them instead; an earlier period's base passes by design and the nightly synchronization rewrites it. The run names the provider faces, the source files behind them and the priced models it compared — one face may spread its declarations over several files, so both counts are reported — and it fails when no priced model was compared.
- **Nightly rewrite.** `scripts/model_registry/update_model_pricing.py` reconciles strict official-source adapters with the checked-in archive, then rewrites the declarations described above from it and checks every period, tier, and window for drift. Automation proposes reviewable updates and gates on column markers and the footnote bodies they cite; runtime pricing stays deterministic, network-free. Only DeepSeek has an official-source adapter; every other vendor stays a reviewed catalog.

- Key deps: [[pricing.meiva.okf.md]] (the node this was split out of)

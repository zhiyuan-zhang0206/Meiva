---
type: doc
title: "meizel/ — Checker Diagnostics"
description: "Every code the checker reports, the rule each one belongs to, and how an edge that closes a cycle names its way out."
tags:
  - locality
  - tool
---

# meizel/ — Checker Diagnostics

These codes are the checker's whole vocabulary: they name everything any rule in `meizel` can
report, and they outlive any single directory, because one run reports edges, purity, ambient reads,
patches, policy and manifests together. That is why this node hangs off the meizel overview rather
than off the checker node, which owns the rules rather than the words they report.

## When it fails

Diagnostic codes: `undeclared-edge`, `not-exported`, `private-module`, `private-name`,
`cycle-edge`, `restricted-python`, the patch codes and `missing-patch-target` ([[meizel/docs/checker/patching/patching.meiva.okf.md|patching]]),
the 15 purity and bounded-container codes ([[meizel/docs/checker/purity.meiva.okf.md|purity]]),
`ambient-read`, `legal-read-recorded`, `stale-root`, `stale-reader`, `stale-approval`,
`approved-budget` ([[meizel/docs/checker/policy/policy.meiva.okf.md|the approved list's ceiling]]),
`fake-entropy` ([[meizel/docs/checker/fake-entropy.meiva.okf.md|fake entropy]]),
`unresolved-import`, `unlisted-allowance`,
`stale-load`, `stale-external`, `policy-syntax`, `debt-paid`, and the manifest codes
(`manifest-` + syntax, unknown-key, unknown-child, duplicate, self-edge, cycle, export-private,
stale-edge, debt, roles). An edge that closes a cycle says so; the way out is a public door or injection.

Parent: [[meizel/docs/meizel.meiva.okf.md|meizel]].

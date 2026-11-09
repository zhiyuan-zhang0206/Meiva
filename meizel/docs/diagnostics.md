# meizel/ — Checker Diagnostics

These codes are the checker's whole vocabulary: they name everything any rule in `meizel` can
report, and they outlive any single directory, because one run reports edges, purity, ambient reads,
patches, policy and manifests together. That is why this node hangs off the meizel overview rather
than off the checker node, which owns the rules rather than the words they report.

## When it fails

Diagnostic codes: `undeclared-edge`, `not-exported`, `private-module`, `private-name`,
`cycle-edge`, `restricted-python`, the patch codes and `missing-patch-target` ([patching](checker/patching/patching.md)),
the 15 purity and bounded-container codes ([purity](checker/purity.md)),
`ambient-read`, `legal-read-recorded`, `stale-root`, `stale-reader`, `stale-approval`,
`approved-budget` ([the approved list's ceiling](checker/policy/policy.md)),
`fake-entropy` ([fake entropy](checker/fake-entropy.md)),
`unresolved-import`, `unlisted-allowance`,
`stale-load`, `stale-external`, `stale-dynamic-attrs`, `policy-syntax`, `debt-paid`, `bare-debt`, and the manifest codes
(`manifest-` + syntax, unknown-key, unknown-child, duplicate, self-edge, cycle, export-private,
stale-edge, debt, roles). An edge that closes a cycle says so; the way out is a public door or injection.

Parent: [meizel](meizel.md).

# Keep Claude convergence aligned with the accepted runtime

Host startup had restored the older pinned CLI after the operator had selected
Claude 2.1.322 for a newly accepted model. Pinning the accepted version prevents
the next startup from undoing that choice. The operator updates native CLI
artifacts and refreshes the gateway's explicit harness probe snapshots separately;
changing this catalog alone does not prove the running host has converged.

This change preserves the existing exact-version policy and credential stores.
It does not change model selection or restart active harness sessions.

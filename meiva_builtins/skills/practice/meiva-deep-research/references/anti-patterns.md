# Anti-patterns — the failure list

Reference for the judgment calls the pipeline cannot mechanically enforce. The
root skill keeps the three that cause most of the damage; this file keeps the
full list so a reviewer can check a plan against it.

- **Writing the report before verification** — the report is derived from the
  state; write it last.
- **Holding findings in conversation context** — compaction loses it; the state
  file is the single source of truth.
- **Snippet trust** — a search snippet is not a source; open the page.
- **Secondary-only citations when the primary is reachable** — else say so.
- **Padding** — a report that looks complete with unverified claims is the
  cardinal failure (Mission). List gaps instead.
- **Workers messaging the orchestrator** — files are the handoff (`meiva-dynamic-workflow`).
- **Endless iteration** — every wave targets a concrete gap; the pre-registered budget stops it.
- **Over-spawning** — a sub-question answerable in a few fetches is done by the
  orchestrator itself.
- **Causal language on correlational evidence** — the Phase 0 classification
  governs.
- **Self-assessment sold as verification** — ladder: formal (strongest) →
  process-level → rubric review → self-assessment (weakest); the audit is
  process-level, label claims accordingly.

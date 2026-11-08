// meiva:note
// Switch-cost policy for the conversation surface (tasks #3894 / #3900).
//
// One place names the numbers a steady-state switch budget is made of, so the
// inspector policy and its tests read the same definition.
//
// - INSPECTOR_RETENTION_MS: per-agent inspector cache retention — matches the
//   QueryClient global gcTime default. A switch back inside the window renders
//   the cached snapshot immediately, with no pending state at all; past it the
//   visit is cold again and each pending cell shows the bare spinner.
// - CONVERSATION_RETENTION_MS: per-agent retention for the conversation trio
//   (timeline / token-usage / pending). With the trio retained, a re-attach
//   needs no per-domain refresh read: the shared composed reconcile
//   (agent-reconcile.ts) refreshes all three in one request, and a switch
//   back renders the cached conversation immediately.
// - BACK_VISIT_REVALIDATE_MAX: the most background revalidations one
//   back-visit may fire — the two live inspector reads (current state +
//   statistics; each is independent and fails independently). The widget set
//   is selection-invariant: task events refresh it while open, the 60s
//   interval repairs across gaps, and a reconnect invalidates it — so
//   switching agents alone never refetches it.
//
// Steady-state target the policy serves (batch 1 + batch 2, both landed): at
// most 5 requests per switch between two warm agents with the panel open —
// the conversation side contributes the stream reconnect plus one composed
// reconcile read, the inspector its two back-visit revalidations — and 0 cold
// pending tells on a back-visit (the 2026-09-18 audit measured 9-10 before
// batch 1).
// meiva:link ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md
export const INSPECTOR_RETENTION_MS = 30 * 60_000;
export const CONVERSATION_RETENTION_MS = 30 * 60_000;
export const BACK_VISIT_REVALIDATE_MAX = 2;

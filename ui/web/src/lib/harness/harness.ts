import type { components } from "@/lib/contracts/types-generated";

// meiva:note
// The runtime loop an agent is born on, fixed for its whole life: `meiva` is the built-in runtime, the others run the loop on an external CLI. The spawn form offers this choice and never changes it afterwards; a fork keeps its source's value. `launchable` is the platform's own answer to "can this machine actually start a session on it" - a harness can be installed (available) and still not launchable, and the UI says so rather than letting the choice look usable.
// meiva:link base/agents/docs/harness/harness.meiva.okf.md

export type Harness = components["schemas"]["Harness"];

// meiva:note
// One machine's row: the probe's verdict plus what this build can start.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
export type HarnessAvailability = components["schemas"]["HarnessAvailability"];

export type HarnessStatus = HarnessAvailability["status"];

export const HARNESSES: readonly Harness[] = ["meiva", "claude_code", "codex", "antigravity"];

export const DEFAULT_HARNESS: Harness = "meiva";

// meiva:note
// Display name: the wire value is snake_case, the UI shows the CLI's own name.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
export function harnessLabel(harness: Harness): string {
  switch (harness) {
    case "meiva":
      return "Meiva";
    case "claude_code":
      return "Claude Code";
    case "codex":
      return "Codex";
    case "antigravity":
      return "Antigravity";
  }
}

// meiva:note
// The probe's rows completed to one row per known harness. A harness the gateway did not answer for is `unknown` - never `unavailable` - and the built-in runtime stays selectable because the process that would run it is the one asking.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
export function harnessRows(rows: HarnessAvailability[]): HarnessAvailability[] {
  const byHarness = new Map(rows.map((row) => [row.harness, row]));
  return HARNESSES.map((harness) => byHarness.get(harness) ?? {
    harness,
    status: harness === DEFAULT_HARNESS ? "available" : "unknown",
    reason: harness === DEFAULT_HARNESS ? "" : "this gateway cannot report harness availability yet",
    launchable: harness === DEFAULT_HARNESS,
    // meiva:note
    // A row the gateway did not answer for is never launchable, so its billing
    // never reaches the price column; the built-in runtime is the one row this
    // fallback can leave selectable, and it is metered.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    billing: "api",
  });
}

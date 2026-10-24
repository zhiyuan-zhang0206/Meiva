import {
  DEFAULT_HARNESS,
  HARNESSES,
  type Harness,
  type HarnessAvailability,
  type HarnessStatus,
} from "@/lib/contracts/models";

export {
  DEFAULT_HARNESS,
  HARNESSES,
  type Harness,
  type HarnessAvailability,
  type HarnessStatus,
};

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
    case "kimi_code":
      return "Kimi Code";
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

import { useQueries, useQuery } from "@tanstack/react-query";

import type { HarnessAvailability } from "@/lib/harness/harness";
import { api } from "@/lib/transport/api";

const STALE_MS = 30_000;

// meiva:note
// One machine's probe query, shared by the single-machine hook and the spawn
// form's per-machine fan-out, so a machine never ends up with two cache entries.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
function probeQuery(machine: string) {
  return {
    queryKey: ["harness-availability", machine],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      api.getHarnessAvailability(machine, signal),
    staleTime: STALE_MS,
    retry: false,
  };
}

// meiva:note
// Harness availability is read per machine because the probe runs where the agent would run, not where the gateway does. A gateway that cannot answer yet (no route) yields an empty list, which the picker renders as unknown instead of unavailable - "not known" must not look like "not installed".
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
export function useHarnessAvailability(machine: string | undefined) {
  return useQuery<HarnessAvailability[]>({
    queryKey: ["harness-availability", machine],
    // meiva:note
    // `enabled` gates the call, so the "no machine chosen" branch never runs; it
    // exists so the request needs neither a non-null assertion nor a made-up
    // machine name. An unanswered probe renders as unknown, never as unavailable.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
    queryFn: ({ signal }) =>
      machine === undefined ? Promise.resolve([]) : api.getHarnessAvailability(machine, signal),
    enabled: machine !== undefined,
    staleTime: STALE_MS,
    retry: false,
  });
}

// meiva:note
// The multi-machine spawn list names its target last, so every candidate machine
// must answer for itself: a harness installed on one box says nothing about the
// box the user ends up choosing. Machines with no answer are absent from the map,
// and their rows read as unknown rather than as unavailable.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
export function useHarnessAvailabilityByMachine(machines: readonly string[]) {
  const results = useQueries({ queries: machines.map(probeQuery) });
  const answered = new Map<string, HarnessAvailability[]>();
  machines.forEach((machine, index) => {
    const data = results[index]?.data;
    if (data !== undefined) answered.set(machine, data);
  });
  return answered;
}

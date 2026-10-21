import { useQueries, useQuery } from "@tanstack/react-query";

import type { HarnessAvailability } from "@/lib/harness/harness";
import { api } from "@/lib/transport/api";

// meiva:note
// One reading per page load (user 2026-10-21). The probe is not free: the gateway
// dispatches an RPC to that machine, which stats the harness binaries and reads
// their login stores. So it is taken once when the page loads, by the sidebar's
// warm-up below, and the picker reads that answer for the rest of the session
// instead of asking again on every open — `staleTime: Infinity` is what keeps the
// form's own mount from re-fetching it. Reloading the page asks again.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
function probeQuery(machine: string) {
  return {
    queryKey: ["harness-availability", machine],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      api.getHarnessAvailability(machine, signal),
    staleTime: Infinity,
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
    staleTime: Infinity,
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

// meiva:note
// The page-load warm-up: the sidebar mounts this once, so every agent-runner
// machine has answered before the picker is ever opened, and the form's own
// hooks land on the cache instead of starting a probe the user then waits for.
// It reads the same ["status"] query the picker uses (deduped), and the machine
// list is derived exactly as the picker derives it, so a paused or non-runner
// machine is never asked.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
export function useWarmHarnessAvailability() {
  const { data } = useQuery({
    queryKey: ["status"],
    queryFn: () => api.getSystemStatus(),
    staleTime: 15_000,
  });
  const machines = (data?.cluster.machines ?? [])
    .filter((machine) => machine.serve_agent_runner && machine.paused !== true)
    .map((machine) => machine.name);
  useHarnessAvailabilityByMachine(machines);
}

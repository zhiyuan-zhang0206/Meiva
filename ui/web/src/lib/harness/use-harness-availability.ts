import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import type { HarnessAvailability } from "./harness";
import { useApi } from "../transport/transport-context";
import type { Api } from "../transport/api";

// meiva:note
// GET reads the last explicit check, including an empty initial snapshot. Page
// loads, picker mounts and query invalidation never start a runner probe.
// meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
function snapshotQuery(machine: string | undefined, client: Api) {
  return {
    queryKey: ["harness-availability", machine],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      machine === undefined ? Promise.resolve([]) : client.getHarnessAvailability(machine, signal),
    enabled: machine !== undefined,
    staleTime: Infinity,
    retry: false,
  };
}

export function useHarnessAvailability(machine: string | undefined) {
  const client = useApi();
  return useQuery(snapshotQuery(machine, client));
}

export function useHarnessAvailabilityByMachine(machines: readonly string[]) {
  const client = useApi();
  const results = useQueries({ queries: machines.map((machine) => snapshotQuery(machine, client)) });
  const answered = new Map<string, HarnessAvailability[]>();
  machines.forEach((machine, index) => {
    const data = results[index]?.data;
    if (data !== undefined) answered.set(machine, data);
  });
  return answered;
}

export function useRefreshHarnessAvailability() {
  const client = useApi();
  const cache = useQueryClient();
  return useMutation({
    mutationKey: ["harness-refresh"],
    mutationFn: (machine: string) => client.refreshHarnessAvailability(machine),
    onSuccess: (rows, machine) => cache.setQueryData(["harness-availability", machine], rows),
    retry: false,
  });
}

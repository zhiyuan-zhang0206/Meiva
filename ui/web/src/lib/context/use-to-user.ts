// meiva:note
// The global SSE fold invalidates document queries on root file changes and reconnect. Explicit refresh, mount and window focus re-read the document and attachment stamps; attachments are not recursively watched. A changed attachment version refreshes its resource URL even when the Markdown text is unchanged.
// meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
"use client";

import { useQuery } from "@tanstack/react-query";
import { useApi } from "../transport/transport-context";

export function useToUser(agentId: number | null, references: string[] = []) {
  const client = useApi();
  return useQuery({
    queryKey: references.length ? ["to-user", agentId, ...references] : ["to-user", agentId],
    queryFn: ({ signal }) => {
      if (agentId == null) throw new Error("To user requires an agent");
      return client.getToUser(agentId, signal, references);
    },
    enabled: agentId != null,
    staleTime: Infinity,
    retry: false,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
}

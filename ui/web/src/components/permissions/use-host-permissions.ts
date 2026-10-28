"use client";

import { useQuery } from "@tanstack/react-query";

import type { HostPermissionsPayload } from "@/lib/contracts/types";
import { useApi } from "@/lib/transport/transport-context";

export const HOST_PERMISSIONS_QUERY_KEY = ["host-permissions"] as const;

export function useHostPermissions() {
  const client = useApi();
  return useQuery({
    queryKey: HOST_PERMISSIONS_QUERY_KEY,
    queryFn: ({ signal }) => client.getHostPermissions(signal),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function isRestricted(payload: HostPermissionsPayload | undefined): boolean {
  if (!payload) return false;
  return payload.state === "action_required" || payload.state === "deferred" || payload.state === "unavailable";
}

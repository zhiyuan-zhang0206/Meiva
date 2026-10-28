"use client";

import { useQuery } from "@tanstack/react-query";

import { useApi } from "../transport/transport-context";
import type { UiContributionsResponse } from "../contracts/types";

export const UI_CONTRIBUTIONS_QUERY_KEY = ["ui-contributions"] as const;

export function useUiContributions(): {
  contributions: UiContributionsResponse | undefined;
  isLoading: boolean;
} {
  const client = useApi();
  const { data, isLoading } = useQuery({
    queryKey: UI_CONTRIBUTIONS_QUERY_KEY,
    queryFn: () => client.getUiContributions(),
  });
  return { contributions: data, isLoading };
}

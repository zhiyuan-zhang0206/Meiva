"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "../transport/api";
import type { UiContributionsResponse } from "../contracts/types";

export const UI_CONTRIBUTIONS_QUERY_KEY = ["ui-contributions"] as const;

export function useUiContributions(): {
  contributions: UiContributionsResponse | undefined;
  isLoading: boolean;
} {
  const { data, isLoading } = useQuery({
    queryKey: UI_CONTRIBUTIONS_QUERY_KEY,
    queryFn: () => api.getUiContributions(),
  });
  return { contributions: data, isLoading };
}

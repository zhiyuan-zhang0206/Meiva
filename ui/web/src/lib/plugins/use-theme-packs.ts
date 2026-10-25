"use client";

import type { UiThemeContribution } from "../contracts/types";
import { useUiContributions } from "./ui-contributions";

export function themePackId(theme: UiThemeContribution): string {
  return `${theme.plugin}/${theme.name}`;
}

/** Every theme pack the cluster's enabled plugins contribute. */
export function useThemePacks(): {
  packs: UiThemeContribution[];
  isLoading: boolean;
} {
  const { contributions, isLoading } = useUiContributions();
  return { packs: contributions?.themes ?? [], isLoading };
}

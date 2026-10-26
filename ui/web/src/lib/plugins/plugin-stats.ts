"use client";

import { useMemo } from "react";

import type { PluginStat, UiStatContribution } from "../contracts/types";
import { systemWallNowDate } from "../transport/system-transport";
import {
  buildPluginStatCardsAt,
  PLUGIN_STAT_STALE_AFTER_MS,
  type PluginStatCard,
  type PluginStatStatus,
} from "./plugins-plan";
import { useUiContributions } from "./ui-contributions";

export {
  PLUGIN_STAT_STALE_AFTER_MS,
  type PluginStatCard,
  type PluginStatStatus,
};

/** Join declared cards with their runtime values, declaration order kept. */
export function buildPluginStatCards(
  declarations: UiStatContribution[] | undefined,
  values: PluginStat[] | undefined,
  now: Date = systemWallNowDate(),
): PluginStatCard[] {
  return buildPluginStatCardsAt(declarations, values, now.getTime());
}

/** The panel's view of the plugin cards: declarations joined with the values
 *  the dashboard response carried. */
export function usePluginStatCards(
  values: PluginStat[] | undefined,
  now: Date = systemWallNowDate(),
): PluginStatCard[] {
  const { contributions } = useUiContributions();
  const declarations = contributions?.stats;
  const nowMs = now.getTime();
  return useMemo(() => buildPluginStatCardsAt(declarations, values, nowMs), [declarations, values, nowMs]);
}

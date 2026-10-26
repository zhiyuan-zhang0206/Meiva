"use client";

import type { PluginStat, UiStatContribution } from "../contracts/types";
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
  now: Date,
): PluginStatCard[] {
  return buildPluginStatCardsAt(declarations, values, now.getTime());
}

/** The panel's view of the plugin cards: declarations joined with the values
 *  the dashboard response carried. */
export function usePluginStatCards(
  values: PluginStat[] | undefined,
  now: Date,
): PluginStatCard[] {
  const { contributions } = useUiContributions();
  const declarations = contributions?.stats;
  return buildPluginStatCards(declarations, values, now);
}

"use client";

import { API_BASE } from "../transport/api";
import type { UiNavContribution } from "../contracts/types";
import {
  filterPluginNav,
  NAV_LOCATIONS,
  pluginPageRoute,
  pluginPageSrcForBase,
  type NavLocation,
} from "./plugins-plan";
import { useUiContributions } from "./ui-contributions";

export { NAV_LOCATIONS, pluginPageRoute, type NavLocation };

/** The nav entries declared for one console surface, in plugin order. */
export function usePluginNav(location: NavLocation): UiNavContribution[] {
  const { contributions } = useUiContributions();
  return filterPluginNav(contributions, location);
}

/** The gateway URL the iframe loads — the plugin's own mount.
 *
 * Absolute (API_BASE), because the console and the gateway are different
 * origins in the default deployment; that also means the framed document gets
 * the gateway's origin rather than the console's. */
export function pluginPageSrc(plugin: string, page: string): string {
  return pluginPageSrcForBase(API_BASE, plugin, page);
}

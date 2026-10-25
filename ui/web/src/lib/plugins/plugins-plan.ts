import type {
  PluginStat,
  UiContributionsResponse,
  UiNavContribution,
  UiStatContribution,
} from "../contracts/types";

export const NAV_LOCATIONS = ["sidebar", "settings", "fleet-toolbar"] as const;
export type NavLocation = (typeof NAV_LOCATIONS)[number];

export function filterPluginNav(
  contributions: UiContributionsResponse | undefined,
  location: NavLocation,
): UiNavContribution[] {
  return (contributions?.nav ?? []).filter((entry) => entry.location === location);
}

export function pluginPageRoute(plugin: string, page: string): `/plugin/${string}/${string}` {
  return `/plugin/${encodeURIComponent(plugin)}/${encodePagePath(page)}`;
}

export function pluginPageSrcForBase(apiBase: string, plugin: string, page: string): string {
  return `${apiBase}/api/plugin-ui/${encodeURIComponent(plugin)}/${encodePagePath(page)}`;
}

function encodePagePath(page: string): string {
  return page.split("/").map(encodeURIComponent).join("/");
}

export const PLUGIN_STAT_STALE_AFTER_MS = 30 * 60 * 1000;

export type PluginStatStatus = PluginStat["status"];

export interface PluginStatCard {
  key: string;
  label: string;
  value: string | null;
  status: PluginStatStatus | null;
  detail: string | null;
  updatedAt: string | null;
  stale: boolean;
}

export function buildPluginStatCardsAt(
  declarations: UiStatContribution[] | undefined,
  values: PluginStat[] | undefined,
  nowMs: number,
): PluginStatCard[] {
  const byKey = new Map<string, PluginStat>();
  for (const value of values ?? []) byKey.set(`${value.plugin}/${value.id}`, value);

  return (declarations ?? []).map((declaration) => {
    const value = byKey.get(`${declaration.plugin}/${declaration.id}`);
    const updatedMs = value !== undefined ? Date.parse(value.updated_at) : Number.NaN;
    return {
      key: `${declaration.plugin}/${declaration.id}`,
      label: declaration.label,
      value: value?.value ?? null,
      status: value?.status ?? null,
      detail: value?.detail ?? null,
      updatedAt: value?.updated_at ?? null,
      stale:
        value !== undefined &&
        Number.isFinite(updatedMs) &&
        nowMs - updatedMs > PLUGIN_STAT_STALE_AFTER_MS,
    };
  });
}

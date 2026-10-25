import { USER_SETTING_DEFAULTS } from "../contracts/types";

export const SETTINGS_QUERY_KEY = ["user-settings"] as const;

export function defaultUserSettings(): Record<string, unknown> {
  return { ...USER_SETTING_DEFAULTS };
}

export function mergeUserSettings(
  entries: readonly { key: string; value: unknown }[],
): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...USER_SETTING_DEFAULTS };
  for (const s of entries) {
    merged[s.key] = s.value;
  }
  return merged;
}

export function withOptimisticSetting(
  old: Record<string, unknown> | undefined,
  key: string,
  value: unknown,
): Record<string, unknown> {
  return {
    ...(old ?? USER_SETTING_DEFAULTS),
    [key]: value,
  };
}

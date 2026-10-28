"use client";

import { useCallback, useSyncExternalStore } from "react";

import { USER_SETTING_DEFAULTS } from "../contracts/types";

let store: Record<string, unknown> = { ...USER_SETTING_DEFAULTS };
let loading = false;
const listeners = new Set<() => void>();
const setSettingSpy: { key: string; value: unknown }[] = [];

function emit(): void {
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): Record<string, unknown> {
  return store;
}

export function resetMockSettings(initial: Record<string, unknown> = {}): void {
  store = { ...USER_SETTING_DEFAULTS, ...initial };
  loading = false;
  setSettingSpy.length = 0;
  emit();
}

export function setMockSetting(key: string, value: unknown): void {
  store = { ...store, [key]: value };
  setSettingSpy.push({ key, value });
  emit();
}

export function mockSetSettingCalls(): readonly { key: string; value: unknown }[] {
  return setSettingSpy;
}

export function useUserSettings(): {
  settings: Record<string, unknown>;
  setSetting: (key: string, value: unknown) => void;
  isLoading: boolean;
} {
  const settings = useSyncExternalStore(subscribe, snapshot, snapshot);
  const setSetting = useCallback((key: string, value: unknown) => setMockSetting(key, value), []);
  return { settings, setSetting, isLoading: loading };
}

export function useDebouncedSetting<T>(
  key: string,
  defaultValue: T,
): readonly [T, (value: T) => void] {
  const settings = useSyncExternalStore(subscribe, snapshot, snapshot);
  const stored = settings[key] as T | undefined;
  const value = stored ?? defaultValue;
  const setValue = useCallback((v: T) => setMockSetting(key, v), [key]);
  return [value, setValue] as const;
}

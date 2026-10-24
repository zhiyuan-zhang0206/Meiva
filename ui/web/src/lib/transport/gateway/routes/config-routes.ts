export function configPath(machine?: string): string {
  return `/api/config${machine != null ? `?machine=${encodeURIComponent(machine)}` : ""}`;
}

export function resolvedConfigPath(model?: string): string {
  return `/api/config/resolved${model != null ? `?model=${encodeURIComponent(model)}` : ""}`;
}

export function inventoryPath(machine: string): string {
  return `/api/inventory?machine=${encodeURIComponent(machine)}`;
}

export function settingPath(key: string): string {
  return `/api/settings/${encodeURIComponent(key)}`;
}

export function harnessAvailabilityPath(machine: string): string {
  return `/api/cluster/machines/${encodeURIComponent(machine)}/harnesses`;
}

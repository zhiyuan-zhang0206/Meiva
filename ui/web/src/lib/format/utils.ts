import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export type ClockFormat = "24h" | "12h";

// meiva:note
// DEFAULT_CLOCK_FORMAT is 24-hour: the AM/PM suffix is noise beside 24-hour logs, so 12-hour is opt-in (user ruling 2026-10-19).
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export const DEFAULT_CLOCK_FORMAT: ClockFormat = "24h";

export function normalizeDayPeriodSpace(value: string): string {
  return value.replace(/[\u202f\u00a0]/g, " ");
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function isRasterImageType(contentType: string): boolean {
  const base = contentType.split(";", 1)[0].trim().toLowerCase();
  return (
    base === "image/png" ||
    base === "image/jpeg" ||
    base === "image/gif" ||
    base === "image/webp" ||
    base === "image/avif" ||
    base === "image/bmp"
  );
}

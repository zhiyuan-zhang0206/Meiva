"use client";

// meiva:note
// useTimeFormat binds the viewer's clock preferences to time.ts: `display.clock_format` ("24h" default, or "12h") drives every clock render, and `display.show_timezone` (default off) appends the zone name to formatAbsolute. A missing key is the default, not an error — user_settings stores only what the viewer changed, so an old row renders like a fresh one; an unrecognized value falls back the same way because the gateway stores values opaquely and a bad row must not take the console down.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md

import { useMemo } from "react";

import { useUserSettings } from "@/lib/state/use-user-settings";

import {
  DEFAULT_CLOCK_FORMAT,
  formatAbsolute,
  formatShort,
  formatUpcoming,
  type ClockFormat,
  type FormatAbsoluteOptions,
  type FormatShortOptions,
  type FormatUpcomingOptions,
} from "./time";

function isClockFormat(value: unknown): value is ClockFormat {
  return value === "24h" || value === "12h";
}

export interface TimeFormat {
  clockFormat: ClockFormat;
  showTimeZone: boolean;
  formatAbsolute: (
    iso: string,
    opts?: Omit<FormatAbsoluteOptions, "clockFormat" | "showTimeZone">,
  ) => string;
  formatShort: (iso: string, opts?: Omit<FormatShortOptions, "clockFormat">) => string;
  formatUpcoming: (
    iso: string | Date,
    now?: Date,
    opts?: Omit<FormatUpcomingOptions, "clockFormat">,
  ) => string;
}

export function useTimeFormat(): TimeFormat {
  const { settings } = useUserSettings();
  const storedClock = settings["display.clock_format"];
  const clockFormat = isClockFormat(storedClock) ? storedClock : DEFAULT_CLOCK_FORMAT;
  const showTimeZone = settings["display.show_timezone"] === true;

  return useMemo(
    () => ({
      clockFormat,
      showTimeZone,
      formatAbsolute: (iso, opts = {}) => formatAbsolute(iso, { ...opts, clockFormat, showTimeZone }),
      formatShort: (iso, opts = {}) => formatShort(iso, { ...opts, clockFormat }),
      formatUpcoming: (iso, now = new Date(), opts = {}) =>
        formatUpcoming(iso, now, { ...opts, clockFormat }),
    }),
    [clockFormat, showTimeZone],
  );
}

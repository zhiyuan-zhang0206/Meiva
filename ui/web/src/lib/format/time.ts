// meiva:note
// This module is the single owner of turning an ISO instant into text a person reads; it replaced nine drifting implementations at the tz audit (2026-08). Views see BROWSER-LOCAL time, a deliberate divergence from the cluster `MEIVA_TIMEZONE` an agent sees — consistency means one wording scheme per audience, not one clock for every audience. Three shapes, one job each: `formatAbsolute` (full precision, zone name optional) for a dedicated "when exactly" slot, `formatShort` (compact, no zone) for space-constrained rows, and `formatRelative` (one signed wording, `in 4m` / `5m ago` / `just now`) for every relative-time surface.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md

// meiva:note
// Clock times are written in one of two formats, chosen by the viewer's `display.clock_format` setting and 24-hour by default: `15:45` or `3:45 PM`. `formatClockText` is the single owner of that choice for the viewer's local zone; every formatter here takes `clockFormat` so no call site can hard-code a 12-hour clock. `formatAbsolute` appends the zone name only when `showTimeZone` is on (setting `display.show_timezone`, default off): the viewer's own zone is implied context, and a bare offset is noise.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md

export type ClockFormat = "24h" | "12h";

// meiva:note
// DEFAULT_CLOCK_FORMAT is 24-hour: the AM/PM suffix is noise beside 24-hour logs, so 12-hour is opt-in (user ruling 2026-10-19).
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export const DEFAULT_CLOCK_FORMAT: ClockFormat = "24h";

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

// meiva:note
// ClockTextOptions picks the clock reading and its precision: `seconds` appends `:SS`, and `milliseconds` appends `.mmm` (implying seconds).
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export interface ClockTextOptions {
  clockFormat?: ClockFormat;
  seconds?: boolean;
  milliseconds?: boolean;
}

// meiva:note
// formatClockText renders the viewer-local wall clock and is the one place the 12/24-hour choice is made: `15:45` in 24-hour or `3:45 PM` in 12-hour, with optional seconds and milliseconds for the run-timeline axis.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export function formatClockText(d: Date, opts: ClockTextOptions = {}): string {
  const { clockFormat = DEFAULT_CLOCK_FORMAT, seconds = false, milliseconds = false } = opts;
  const hour24 = d.getHours();
  const twelveHour = clockFormat === "12h";
  const hour = twelveHour ? (hour24 % 12 === 0 ? 12 : hour24 % 12) : hour24;
  let text = `${twelveHour ? hour : pad2(hour)}:${pad2(d.getMinutes())}`;
  if (seconds || milliseconds) text += `:${pad2(d.getSeconds())}`;
  if (milliseconds) text += `.${String(d.getMilliseconds()).padStart(3, "0")}`;
  if (twelveHour) text += hour24 < 12 ? " AM" : " PM";
  return text;
}

// meiva:note
// FormatAbsoluteOptions: `weekday` inserts the abbreviated weekday between date and time, `clockFormat` chooses 24- or 12-hour (default 24), and `showTimeZone` appends the short zone name (`GMT+8`, `PDT`, `UTC`; default off).
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export interface FormatAbsoluteOptions {
  weekday?: boolean;
  clockFormat?: ClockFormat;
  showTimeZone?: boolean;
}

// meiva:note
// formatAbsolute renders the viewer-local full datetime with seconds: `2026-07-25 02:53:29`, `2026-07-25 Sat 02:53:29` with the weekday, `2:53:29 AM` on the 12-hour clock, and a trailing zone name only when asked. The zone name is requested without any time field, so formatToParts yields date parts plus the zone while the clock comes from formatClockText, the one 12/24-hour implementation. An unparseable iso string yields an empty string; the caller substitutes its own placeholder.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export function formatAbsolute(iso: string, opts: FormatAbsoluteOptions = {}): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const { weekday = false, clockFormat = DEFAULT_CLOCK_FORMAT, showTimeZone = false } = opts;
  const parts = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(weekday ? { weekday: "short" } : {}),
    ...(showTimeZone ? { timeZoneName: "short" } : {}),
  }).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const datePart = `${get("year")}-${get("month")}-${get("day")}`;
  const weekdayPart = weekday ? ` ${get("weekday")}` : "";
  const timePart = formatClockText(d, { clockFormat, seconds: true });
  const zonePart = showTimeZone ? ` ${get("timeZoneName")}` : "";
  return `${datePart}${weekdayPart} ${timePart}${zonePart}`;
}

// meiva:note
// FormatShortOptions: `includeDate` prefixes `M/D` (default true; false gives a bare clock cell for a time today), and `clockFormat` chooses 24- or 12-hour (default 24).
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export interface FormatShortOptions {
  includeDate?: boolean;
  clockFormat?: ClockFormat;
}

// meiva:note
// formatShort renders the compact form `7/12 15:45` (`1:45 PM` on the 12-hour clock), or a bare wall-clock cell with `includeDate: false`, for space-constrained rows where the viewer's own clock is implied. An unparseable iso string yields an em dash.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export function formatShort(iso: string, opts: FormatShortOptions = {}): string {
  const { includeDate = true, clockFormat = DEFAULT_CLOCK_FORMAT } = opts;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const clock = formatClockText(d, { clockFormat });
  if (!includeDate) return clock;
  const mo = d.getMonth() + 1;
  const day = d.getDate();
  return `${mo}/${day} ${clock}`;
}

/** Compact elapsed span, floors: `45s` / `2h 14m` / `24d 3h` (the day tier
 *  kicks in past 24h). Used for runtimes and TTL-remaining countdowns. */
export function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH ? `${d}d ${remH}h` : `${d}d`;
}

// meiva:note
// Signed relative time in both directions: `in 4m` (future), `5m ago` (past), and `just now` under a minute; seconds through years. The one wording scheme every relative-time surface in the app uses, so a copy change belongs here rather than at a call site. An unparseable iso string yields an em dash.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export function formatRelative(iso: string, now: Date = new Date()): string {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "—";
  const deltaSec = Math.round((t - now.getTime()) / 1000);
  const mag = Math.abs(deltaSec);
  if (mag < 60) return "just now";
  const min = Math.floor(mag / 60);
  let span: string;
  if (min < 60) span = `${min}m`;
  else if (min < 60 * 24) span = `${Math.floor(min / 60)}h`;
  else if (min < 60 * 24 * 30) span = `${Math.floor(min / 60 / 24)}d`;
  else if (min < 60 * 24 * 365) span = `${Math.floor(min / 60 / 24 / 30)}mo`;
  else span = `${Math.floor(min / 60 / 24 / 365)}y`;
  return deltaSec > 0 ? `in ${span}` : `${span} ago`;
}

const MS_PER_DAY = 86_400_000;
const TOMORROW_DAYS = 1;
const WEEKDAY_DAYS_MAX = 6;
const DATE_DAYS_MIN = 30;

export interface FormatUpcomingOptions {
  timeZone?: string;
  clockFormat?: ClockFormat;
}

// meiva:note
// formatUpcoming renders an upcoming instant as a clock time in the viewer's format (24-hour by default, 12-hour when `clockFormat` says so) plus the day context its viewer-local calendar date requires, and never as a bare clock time for a future day: clock only later today, 'Tomorrow' on the next local date, a weekday name from two to six days, a day count with the date from seven to 29 days, and the date alone from 30 days. The boundary is local midnight, not 24 elapsed hours, so a DST day still counts as one day. A past instant keeps the shared relative wording and an unparseable input yields an em dash.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export function formatUpcoming(
  iso: string | Date,
  now: Date = new Date(),
  opts: FormatUpcomingOptions = {},
): string {
  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) return "—";
  if (target.getTime() <= now.getTime()) return formatRelative(target.toISOString(), now);

  const zone = opts.timeZone;
  const clockFormat = opts.clockFormat ?? DEFAULT_CLOCK_FORMAT;
  const targetDate = localDateFields(target, zone);
  const nowDate = localDateFields(now, zone);
  const days = dayOrdinal(targetDate) - dayOrdinal(nowDate);
  const clock = clockLabel(target, zone, clockFormat);
  const monthDay = monthDayLabel(target, zone, targetDate.year !== nowDate.year);

  if (days <= 0) return clock;
  if (days === TOMORROW_DAYS) return `Tomorrow ${clock}`;
  if (days <= WEEKDAY_DAYS_MAX) return `${intlPart(target, zone, { weekday: "short" })} ${clock}`;
  if (days < DATE_DAYS_MIN) return `In ${days} days (${monthDay})`;
  return monthDay;
}

interface LocalDateFields {
  year: number;
  month: number;
  day: number;
}

function intlFormatter(
  timeZone: string | undefined,
  options: Intl.DateTimeFormatOptions,
): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-US", { ...options, timeZone });
}

function intlPart(
  d: Date,
  timeZone: string | undefined,
  options: Intl.DateTimeFormatOptions,
): string {
  return intlFormatter(timeZone, options).format(d);
}

function localDateFields(d: Date, timeZone?: string): LocalDateFields {
  const parts = intlFormatter(timeZone, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const num = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return { year: num("year"), month: num("month"), day: num("day") };
}

function dayOrdinal({ year, month, day }: LocalDateFields): number {
  return Math.round(Date.UTC(year, month - 1, day) / MS_PER_DAY);
}

export function normalizeDayPeriodSpace(value: string): string {
  return value.replace(/[\u202f\u00a0]/g, " ");
}

function clockLabel(d: Date, timeZone: string | undefined, clockFormat: ClockFormat): string {
  const twelveHour = clockFormat === "12h";
  return normalizeDayPeriodSpace(
    intlPart(d, timeZone, {
      hour: twelveHour ? "numeric" : "2-digit",
      minute: "2-digit",
      hour12: twelveHour,
    }),
  );
}

function monthDayLabel(d: Date, timeZone: string | undefined, withYear: boolean): string {
  return intlPart(d, timeZone, {
    month: "short",
    day: "numeric",
    ...(withYear ? { year: "numeric" } : {}),
  });
}

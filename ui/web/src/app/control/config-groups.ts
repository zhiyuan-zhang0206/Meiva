// Frontend-side display grouping + tag derivation for the Config section.
//
// The backend serves 11 coarse domain groups (LLM / Agent / Daemon / …); the
// Control page displays 17 finer semantic groups (per the approved control
// prototype). The regrouping is pure presentation, so it lives here as a
// static env-var → group map rather than an API change. Fields the map does
// not know (added after this file) fall back to a per-backend-group default,
// and a completely unknown backend group lands in a trailing "Other" bucket —
// nothing ever disappears from the panel.
//
// Group ids double as element ids / URL anchors and MUST match the Config
// sub-entries in _sections.ts (the nav links jump to them).

import type { ConfigFieldView } from "@/lib/contracts/types";

import { GROUP_ENV_VARS } from "./config-group-defs";

export { CONFIG_DISPLAY_GROUPS, CONFIG_FILTERS, DEFAULT_MODEL_GROUP_ID, GROUP_ENV_VARS, HIDDEN_ENV_VARS, PER_MODEL_GROUP_ID } from "./config-group-defs";

const OTHER_GROUP_ID = "config-other";

// A field not in the static map is grouped by its backend domain group. Every
// current backend group has a home; new backend groups land in "Other".
const BACKEND_GROUP_FALLBACK: Record<string, string> = {
  LLM: "config-llm",
  Sandbox: "config-exec",
  Agent: "config-exec",
  Web: "config-web",
  Gateway: "config-gateway",
  Daemon: "config-daemon-tasks",
  "Data plane": "config-dataplane",
  Services: "config-services",
  Observability: "config-observability",
  Telegram: "config-web",
  General: "config-general",
  // Display windows/limits (backend domain "Display", task #3696) — the
  // "Display & general" bucket is their home.
  Display: "config-general",
};

// env var → group id, inverted once from the per-group lists.
const ENV_TO_GROUP = new Map<string, string>(
  Object.entries(GROUP_ENV_VARS).flatMap(([gid, envs]) => envs.map((e) => [e, gid] as const)),
);

// env var → index within its group (the prototype's curated order).
const ENV_ORDER = new Map<string, number>(
  Object.values(GROUP_ENV_VARS).flatMap((envs) => envs.map((e, i) => [e, i] as const)),
);

/** Display group id for a field: static map > backend-group fallback > Other. */
export function displayGroupId(field: ConfigFieldView): string {
  const fallback = (BACKEND_GROUP_FALLBACK as Partial<Record<string, string>>)[field.group];
  return ENV_TO_GROUP.get(field.env_var) ?? fallback ?? OTHER_GROUP_ID;
}

/** Curated position of a field inside its display group; unmapped fields sort
 *  after every mapped one (then alphabetically, applied by the caller). */
export function displayOrder(field: ConfigFieldView): number {
  return ENV_ORDER.get(field.env_var) ?? Number.MAX_SAFE_INTEGER;
}

/** Display label from the env var: strip the MEIVA_ prefix and turn underscores
 *  into spaces, preserving the original case so abbreviations stay intact
 *  (MEIVA_MODEL → "MODEL", MEIVA_LLM_OVERRIDE → "LLM OVERRIDE", DEEPSEEK_API_KEY →
 *  "DEEPSEEK API KEY"). No case conversion — env vars are uppercase by
 *  convention, so labels read uppercase and never mangle LLM/SSE/URL/CIDR. */
export function fieldLabel(envVar: string): string {
  return envVar.replace(/^MEIVA_/, "").split("_").filter(Boolean).join(" ");
}

/** Whether individual agents can override this field via spawn/restart
 *  config overlay — drives the "per-agent" tag and the per-agent filter.
 *  Served by the backend as `ConfigFieldView.per_agent` (the same flag the
 *  per-model resolution view uses); no scope-approximation or extra-env table. */
export function isPerAgent(field: ConfigFieldView): boolean {
  return field.per_agent;
}

/**
 * Render a config value for display — shared by the editable field rows and the
 * per-model resolution view so one value never reads two ways.
 *
 * Values arrive typed `object` on the wire (`base/api_contracts/config.py`),
 * so a field the backend ever grows a structured (list/dict) value for lands
 * here as-is. `String()` on a plain object is always the content-free
 * "[object Object]"; stringify it instead so such a field still shows something
 * readable rather than that literal.
 */
export function formatConfigValue(v: unknown): string {
  if (v == null || v === "") return "(empty)";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      // Circular structure — no better fallback than the base toString.
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      return String(v);
    }
  }
  // Every remaining `unknown` case (string/number/boolean/bigint/etc.) is a
  // primitive whose toString is its own meaningful representation, not the
  // object default — the lint rule can't see that the `object` branch above
  // already returned.
  // eslint-disable-next-line @typescript-eslint/no-base-to-string
  return String(v);
}

/** The filterable tag set of a field. `editable` is the row's effective
 *  editability under the current machine selection. A writable secret (an API
 *  key) is editable write-only, so it tags "runtime" + "sensitive"; a
 *  non-writable secret (db_url) tags "cli-only" + "sensitive". */
export function fieldFilterTags(field: ConfigFieldView, editable: boolean): Set<string> {
  const tags = new Set<string>([field.scope, editable ? "runtime" : "cli-only"]);
  if (field.restart_required) tags.add("startup");
  if (isPerAgent(field)) tags.add("per-agent");
  if (field.sensitive) tags.add("sensitive");
  return tags;
}

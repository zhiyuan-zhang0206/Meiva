import { CONTROL_SECTIONS } from "./sections";

const OTHER_GROUP_ID = "config-other";

// The per-model resolution view is a nav sub-entry but NOT a field bucket: it
// renders its own element under this id (_per_model.tsx), so it is excluded
// from the field-group list below — otherwise the id would exist twice and the
// nav anchor would land on whichever came first.
export const PER_MODEL_GROUP_ID = "config-per-model";

// Same deal: the cluster default-model control renders its own element
// (_default_model.tsx) and edits a DB row, not a `.env` field, so it is a nav
// sub-entry but never a field bucket.
export const DEFAULT_MODEL_GROUP_ID = "config-default-model";

// Display order INSIDE each group is the array order here (the prototype's
// curated order); unmapped fields append after, alphabetically.
// Exported for the consistency test (HIDDEN_ENV_VARS ∩ GROUP_ENV_VARS == ∅).
export const GROUP_ENV_VARS: Record<string, string[]> = {
  "config-llm": [
    "MEIVA_MODEL",
    "MEIVA_LABELER_MODEL",
    "MEIVA_HIERARCHY_MODEL",
    "MEIVA_REASONING_EFFORT",
    "MEIVA_DASHSCOPE_BASE_URL",
    "MEIVA_UNDERSTAND_TEXT_MODEL",
    "MEIVA_UNDERSTAND_MEDIA_MODEL",
    "MEIVA_UNDERSTAND_MEDIA_RESOLUTION",
    "MEIVA_UNDERSTAND_MEDIA_THINKING_LEVEL",
    "MEIVA_UNDERSTAND_MEDIA_BASE_URL",
    "MEIVA_LLM_STREAM_TTFT_TIMEOUT_SECONDS",
    "MEIVA_LLM_STREAM_INTER_CHUNK_TIMEOUT_SECONDS",
    "MEIVA_LLM_RETRY_MAX_ATTEMPTS",
    "MEIVA_LLM_RETRY_INITIAL_INTERVAL_SECONDS",
    "MEIVA_LLM_RETRY_MAX_INTERVAL_SECONDS",
    "MEIVA_LLM_FATAL_PROVIDER_ERROR_TYPES",
    "MEIVA_LLM_OVERRIDE",
  ],
  "config-prompts": [
    "MEIVA_SYSTEM_PROMPT_EXTRA",
    "MEIVA_SYSTEM_PROMPT_SDK_OVERVIEW",
    "MEIVA_SYSTEM_PROMPT_PREFER_SDK",
    "MEIVA_SYSTEM_PROMPT_CODEACT",
    "MEIVA_SYSTEM_PROMPT_SEQUENTIAL_TOOL_CALLS",
    "MEIVA_AGENT_COMMUNICATION_STYLE",
    "MEIVA_SYSTEM_PROMPT_MEMORY",
    "MEIVA_SYSTEM_PROMPT_CONCISENESS",
    "MEIVA_SYSTEM_PROMPT_REPORTING",
    "MEIVA_SYSTEM_PROMPT_CAUTION",
    "MEIVA_SYSTEM_PROMPT_ALIGN",
    "MEIVA_SYSTEM_PROMPT_DELEGATION_CHECK",
    "MEIVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION",
    "MEIVA_SYSTEM_PROMPT_FILE_DRIVEN_WORK",
    "MEIVA_SYSTEM_PROMPT_TEMPORAL",
    "MEIVA_SYSTEM_PROMPT_INVEST_FUTURE",
  ],
  "config-exec": [
    "MEIVA_EXEC_TIMEOUT_SECONDS",
    "MEIVA_EXEC_NODE_TIMEOUT_SECONDS",
    "MEIVA_EXEC_OUTPUT_MAX_CHARS",
    "MEIVA_EXEC_OUTPUT_ACCUMULATION_MAX_CHARS",
    "MEIVA_SYNTAX_FIX_RUFF_FORMAT",
    "MEIVA_MCP_CONNECT_TIMEOUT_SECONDS",
    "MEIVA_SECURITY_SCAN_ENABLED",
  ],
  "config-memory": [
    "MEIVA_AUTO_COMPACT_FRACTION",
    "MEIVA_COMPACT_REMINDER_FRACTION",
    "MEIVA_MEMORY_INDEX_INJECT",
    "MEIVA_MEMORY_PER_AGENT_INJECT",
    "MEIVA_MEMORY_PER_AGENT_INDEX_MAX_LINES",
    "MEIVA_PASSIVE_MEMORY_RECALL",
    "MEIVA_AGENT_REPLY_REMINDER_CADENCE",
  ],
  "config-sdk": [
    "MEIVA_SDK_DISABLE",
    "MEIVA_SDK_EXPAND",
    "MEIVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT",
    "MEIVA_SKILLS_TO_EXPAND_AT_START",
    "MEIVA_WORKSPACE_IN_SYSTEM_PROMPT",
    "MEIVA_COMMANDS_ENABLED",
  ],
  "config-agent-infra": [
    "MEIVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS",
    "MEIVA_NODE_STALL_DUMP_SECONDS",
    "MEIVA_DB_POOL_ACQUIRE_TIMEOUT_SECONDS",
  ],
  "config-daemon-heartbeat": [
    "MEIVA_HEARTBEAT_ENABLED",
    "MEIVA_HEARTBEAT_INTERVAL_SECONDS",
    "MEIVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS",
  ],
  "config-daemon-tasks": [
    "MEIVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS",
  ],
  "config-gateway": [
    "MEIVA_GATEWAY_HTTP_TIMEOUT_SECONDS",
    "MEIVA_SSE_DISCONNECT_POLL_SECONDS",
    "MEIVA_SSE_THROTTLE_RATE",
    "MEIVA_GATEWAY_RELOAD",
    "MEIVA_GATEWAY_HUMAN_AUTH",
  ],
  "config-connection": [
    "MEIVA_GATEWAY_PORT",
    "MEIVA_GATEWAY_URL",
    "MEIVA_MACHINE_HOST",
    "MEIVA_MACHINE_SERVE_GATEWAY",
    "MEIVA_MACHINE_SERVE_AGENT_RUNNER",
    "MEIVA_TRUSTED_CIDRS",
  ],
  "config-dataplane": [
    "MEIVA_PGBOUNCER_ENABLED",
    "MEIVA_EVENTS_CHANNEL",
    "MEIVA_DB_SSLMODE",
    "MEIVA_DB_POOL_MIN_SIZE",
    "MEIVA_DB_POOL_MAX_SIZE",
  ],
  "config-security": [
    "MEIVA_DB_URL",
    "MEIVA_REDIS_URL",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "MIMO_API_KEY",
    "MOONSHOT_API_KEY",
    "GLM_API_KEY",
    "DASHSCOPE_API_KEY",
  ],
  "config-general": [
    "MEIVA_TIMEZONE",
    "MEIVA_MESSAGE_TIMESTAMPS",
    "MEIVA_MESSAGE_TIMESTAMP_WEEKDAY",
    "MEIVA_TIMELINE_COMPACT_HISTORY",
    "MEIVA_TRACK_BRANCH",
    "MEIVA_MACHINE_NAME",
    "MEIVA_MACHINE_DESCRIPTION",
    "MEIVA_MEMORY_REMOTE",
    "MEIVA_MEMORY_KEEP_LOCAL",
    "MEIVA_CROSS_MACHINE_TRANSFER_BACKEND",
    "MEIVA_REQUIRE_GITHUB_PR",
  ],
  "config-observability": [
    "MEIVA_TRACE_ENABLED",
    "MEIVA_TRACE_RETENTION_DAYS",
    "MEIVA_TELEMETRY_OTLP_ENABLED",
    "MEIVA_TELEMETRY_OTLP_ENDPOINT",
    "MEIVA_TELEMETRY_OTLP_PORT",
  ],
  "config-web": [
    "MEIVA_WEB_SEARCH_PROVIDER",
    "MEIVA_WEB_SEARCH_TIMEOUT_SECONDS",
    "MEIVA_WEB_MAX_RESULTS",
    "MEIVA_WEB_BRAVE_SEARCH_ENDPOINT",
    "BRAVE_API_KEY",
    "MEIVA_WEB_DEEPSEEK_SEARCH_ENDPOINT",
    "MEIVA_WEB_DEEPSEEK_MAX_RESULTS",
    "MEIVA_WEB_DEEPSEEK_QPS",
    "MEIVA_WEB_FETCH_READER",
    "MEIVA_WEB_FETCH_TIMEOUT_SECONDS",
    "MEIVA_WEB_JINA_BASE_URL",
    "JINA_API_KEY",
    "MEIVA_WEB_DEEPSEEK_FETCH_ENDPOINT",
    "MEIVA_TELEGRAM_BOT_TOKEN",
    "MEIVA_TELEGRAM_OWNER_ID",
  ],
  "config-services": [
    "MEIVA_BROWSER_ENABLED",
    "MEIVA_CHROME_BINARY",
    "MEIVA_BROWSER_CDP_PORT",
    "MEIVA_PERMISSIONS_HELPER_ENABLED",
    "MEIVA_PERMISSIONS_HELPER_PORT",
    "MEIVA_MEMORY_ROOT",
    "MEIVA_PROJECT_ROOT",
    "MEIVA_OPS_CONCURRENCY",
  ],
  "config-health": [
    "MEIVA_FRONTEND_HEALTHCHECK_URL",
    "MEIVA_GATEWAY_HEALTH_URL",
    "MEIVA_LABELER_HEALTH_PORT",
    "MEIVA_HEARTBEAT_HEALTH_PORT",
    "MEIVA_TASK_MAINTENANCE_HEALTH_PORT",
    "MEIVA_EVENTS_MAINTENANCE_HEALTH_PORT",
    "MEIVA_MEMORY_INDEXER_HEALTH_PORT",
    "MEIVA_OPS_HEALTH_PORT",
    "MEIVA_GATEWAY_PIDFILE",
  ],
};

export const HIDDEN_ENV_VARS = new Set<string>([
  "MEIVA_CLUSTER_SECRET",
  "MEIVA_GATEWAY_MAX_RETRIES",
  "MEIVA_GATEWAY_RETRY_DELAY_SECONDS",
]);

// (group id, label) in render order — labels come from _sections.ts so the nav
// links and the on-page headers can never disagree.
export const CONFIG_DISPLAY_GROUPS: { id: string; label: string }[] = [
  ...(CONTROL_SECTIONS.find((s) => s.id === "config")?.subs ?? []).filter(
    (s) => s.id !== PER_MODEL_GROUP_ID && s.id !== DEFAULT_MODEL_GROUP_ID,
  ),
  { id: OTHER_GROUP_ID, label: "Other" },
];

// The tag-filter bar's options, in display order. "all" is the reset pseudo-tag.
export const CONFIG_FILTERS: { id: string; label: string }[] = [
  { id: "all", label: "All" },
  { id: "runtime", label: "Runtime" },
  { id: "cli-only", label: "CLI-only" },
  { id: "startup", label: "Startup" },
  { id: "per-agent", label: "Per-agent" },
  { id: "sensitive", label: "Sensitive" },
  { id: "cluster-pinned", label: "Cluster-pinned" },
  { id: "cluster-default", label: "Cluster-default" },
  { id: "host", label: "Host" },
  { id: "agent", label: "Agent" },
];

// Submit user input from a page opened through Meiva's authenticated page URL.
// The browser uses the existing same-origin login session. These identifiers
// route the input; they are not credentials and do not grant permissions.
// meiva:note
// Template the owning agent id, the registered page name, and the gateway origin
// (__GATEWAY_ORIGIN__, an absolute URL). Display-only
// pages do not need this helper. The widgets embed this body for zero-build use.

const MEIVA_AGENT_ID = "__AGENT_ID__";
const MEIVA_PAGE_NAME = "__PAGE_NAME__";

const MEIVA_GATEWAY_RAW = "__GATEWAY_ORIGIN__";
// meiva:note
// Templated at render time. Left unreplaced it falls back to this page's own origin,
// which is correct while pages are served from the gateway — the one value that changes
// when they move to their own origin (task #6058).
const MEIVA_GATEWAY = MEIVA_GATEWAY_RAW.startsWith("__")
  ? location.origin
  : MEIVA_GATEWAY_RAW.replace(/\/+$/, "");
const MEIVA_REPLY_URL = `${MEIVA_GATEWAY}/pages/${MEIVA_AGENT_ID}-${MEIVA_PAGE_NAME}/reply`;

// meiva:note
// The one write a page may make: a bounded envelope (kind, choices, text) the gateway
// renders into one chat message to the owning agent. See task #6058.
async function meivaReply(kind, choices, text) {
  const res = await fetch(MEIVA_REPLY_URL, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, choices: choices ?? [], text: text ?? "" }),
  });
  if (!res.ok) throw new Error(`meivaReply failed: HTTP ${res.status}`);
  return res.json();
}

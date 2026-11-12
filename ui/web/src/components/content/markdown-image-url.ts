// meiva:note
// Local image bytes use the existing agent-scoped workspace reader. An explicit
// workspace owner travels with an absolute cross-agent reference; the gateway
// still resolves placement and enforces that owner's workspace/cwd boundaries.
export function markdownImageUrl(src: string, agentId: number | null, apiBase: string): string {
  if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(src) && !src.startsWith("file://")) return src;
  if (/^\/(?:api|_next)\//.test(src)) return src;
  let path = src.startsWith("file://") ? src.slice(7) : src;
  // meiva:note
  // Markdown URI normalization percent-encodes spaces and Unicode before the
  // component receives src. Decode once before encoding the API query value.
  try { path = decodeURI(path); } catch { return src; }
  const owned = /(?:^|\/)\.meiva\/workspaces\/([1-9]\d*)\/(.+)$/.exec(path);
  const owner = owned ? owned[1] : agentId;
  if (owner === null || !path) return src;
  return `${apiBase}/api/agents/${owner}/files/raw?path=${encodeURIComponent(owned ? owned[2] : path)}`;
}

// meiva:note
// Local workspace references share one explicit-owner rule across inline images and
// the file viewer: a `.meiva/workspaces/<id>/<subpath>` reference belongs to `<id>`,
// while relative or cwd paths belong to the surrounding agent context. Relative hops
// inside a viewed workspace Markdown file stay pinned to that file's directory and
// workspace prefix without folding `..` across the workspace root boundary, so any
// escape still reaches the gateway boundary and is rejected with 403.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.md

const EXPLICIT_WORKSPACE_RE = /^(.*?(?:^|\/)\.meiva\/workspaces\/([1-9]\d*))\/([\s\S]+)$/;

function stripFileScheme(rawPath: string): string {
  if (!/^file:\/\//i.test(rawPath)) return rawPath;
  const rest = rawPath.slice(7);
  return /^localhost\//i.test(rest) ? rest.slice(9) : rest;
}

function splitSuffix(rawPath: string): { pathPart: string; suffix: string } {
  const idx = rawPath.search(/[?#]/);
  return idx >= 0
    ? { pathPart: rawPath.slice(0, idx), suffix: rawPath.slice(idx) }
    : { pathPart: rawPath, suffix: "" };
}

function decodeOrRaw(value: string): string {
  try {
    return decodeURI(value);
  } catch {
    return value;
  }
}

function joinRelativeSubpath(baseDir: string, relativeRef: string): string {
  const stack = baseDir ? baseDir.split("/").filter((seg) => seg.length > 0 && seg !== ".") : [];
  let leadingParents = 0;
  for (const seg of relativeRef.replace(/\\/g, "/").split("/")) {
    if (!seg || seg === ".") continue;
    if (seg === "..") {
      if (stack.length > 0) {
        stack.pop();
      } else {
        leadingParents += 1;
      }
      continue;
    }
    stack.push(seg);
  }
  const parentPrefix = leadingParents > 0 ? Array<string>(leadingParents).fill("..").join("/") : "";
  if (parentPrefix && stack.length > 0) return `${parentPrefix}/${stack.join("/")}`;
  if (parentPrefix) return parentPrefix;
  return stack.join("/");
}

export function resolveWorkspaceFile<T extends number | null>(
  rawPath: string,
  fallbackAgentId: T,
  { preserveExplicit = false } = {},
): { ownerAgentId: number | T; requestPath: string } {
  const { pathPart } = splitSuffix(rawPath.trim());
  const decoded = decodeOrRaw(stripFileScheme(pathPart));
  const owned = EXPLICIT_WORKSPACE_RE.exec(decoded);
  if (owned) {
    return { ownerAgentId: Number(owned[2]), requestPath: preserveExplicit ? decoded : owned[3] };
  }
  return { ownerAgentId: fallbackAgentId, requestPath: decoded };
}

export function resolveWorkspaceReference(baseFilePath: string, reference: string): string {
  const trimmed = reference.trim();
  if (!trimmed) return reference;
  if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(trimmed)) return trimmed;
  if (trimmed.startsWith("/") || trimmed.startsWith("~/")) return trimmed;

  const { pathPart: refPathPart, suffix: refSuffix } = splitSuffix(trimmed);
  const decodedRef = decodeOrRaw(refPathPart);
  if (EXPLICIT_WORKSPACE_RE.test(decodedRef)) return trimmed;

  const { pathPart: basePathPart } = splitSuffix(stripFileScheme(baseFilePath.trim()));
  const decodedBase = decodeOrRaw(basePathPart);

  const ownedBase = EXPLICIT_WORKSPACE_RE.exec(decodedBase);
  if (ownedBase) {
    const workspacePrefix = ownedBase[1];
    const baseSubpath = ownedBase[3];
    const lastSlash = baseSubpath.lastIndexOf("/");
    const baseDir = lastSlash >= 0 ? baseSubpath.slice(0, lastSlash) : "";
    const joined = joinRelativeSubpath(baseDir, decodedRef);
    return `${workspacePrefix}/${joined}${refSuffix}`;
  }

  if (decodedBase.startsWith("/")) {
    const lastSlash = decodedBase.lastIndexOf("/");
    const baseDir = lastSlash > 0 ? decodedBase.slice(1, lastSlash) : "";
    const joined = joinRelativeSubpath(baseDir, decodedRef);
    return `/${joined}${refSuffix}`;
  }

  const lastSlash = decodedBase.lastIndexOf("/");
  const baseDir = lastSlash >= 0 ? decodedBase.slice(0, lastSlash) : "";
  return `${joinRelativeSubpath(baseDir, decodedRef)}${refSuffix}`;
}

export function markdownImageUrl(src: string, agentId: number | null, apiBase: string): string {
  if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(src) && !/^file:\/\//i.test(src)) return src;
  if (/^\/(?:api|_next)\//.test(src)) return src;
  const withoutScheme = stripFileScheme(src);
  // meiva:note
  // Markdown URI normalization percent-encodes spaces and Unicode before the
  // component receives src. Decode once before encoding the API query value.
  try {
    void decodeURI(withoutScheme);
  } catch {
    return src;
  }
  const { ownerAgentId, requestPath } = resolveWorkspaceFile(src, agentId);
  if (ownerAgentId === null || !requestPath) return src;
  return `${apiBase}/api/agents/${ownerAgentId}/files/raw?path=${encodeURIComponent(requestPath)}`;
}

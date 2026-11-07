// meiva:note
// Utilities to detect workspace file paths in Markdown links and inline code spans,
// extracting paths and line ranges (e.g. #L10 or #L10-L20) for interactive file viewing.

export const COMMON_FILE_EXTENSIONS: readonly string[] = Object.freeze([
  ".py",
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".mjs",
  ".cjs",
  ".json",
  ".md",
  ".markdown",
  ".sql",
  ".sh",
  ".bash",
  ".zsh",
  ".toml",
  ".yaml",
  ".yml",
  ".txt",
  ".css",
  ".scss",
  ".html",
  ".xml",
  ".csv",
  ".env",
  ".rs",
  ".go",
  ".c",
  ".cpp",
  ".h",
  ".java",
  ".kt",
  ".rb",
  ".php",
  ".svg",
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".ico",
  ".mp4",
  ".webm",
  ".mov",
  ".mkv",
  ".mp3",
  ".wav",
  ".ogg",
  ".m4a",
  ".flac",
  ".pdf",
  ".lock",
  ".log",
  ".diff",
  ".patch",
]);

export const IMAGE_EXTENSIONS: readonly string[] = Object.freeze([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".svg",
  ".bmp",
  ".ico",
]);

export const VIDEO_EXTENSIONS: readonly string[] = Object.freeze([
  ".mp4",
  ".webm",
  ".mov",
  ".mkv",
]);

export const AUDIO_EXTENSIONS: readonly string[] = Object.freeze([
  ".mp3",
  ".wav",
  ".ogg",
  ".m4a",
  ".flac",
]);

export function isImageExtension(ext: string): boolean {
  return IMAGE_EXTENSIONS.includes(ext.toLowerCase());
}

export function isVideoExtension(ext: string): boolean {
  return VIDEO_EXTENSIONS.includes(ext.toLowerCase());
}

export function isAudioExtension(ext: string): boolean {
  return AUDIO_EXTENSIONS.includes(ext.toLowerCase());
}

export function isPdfExtension(ext: string): boolean {
  return ext.toLowerCase() === ".pdf";
}

const LINE_RANGE_RE = /#L?(\d+)(?:-L?(\d+))?$/i;
const DANGEROUS_PROTOCOLS = /^(javascript|vbscript):/i;
const WEB_URL_RE = /^https?:\/\//i;

export interface FilePathMatch {
  isPath: boolean;
  path: string;
  lineRange?: string;
  filename: string;
  extension: string;
}

export function parseLineRange(lineRange?: string): { start: number; end: number } | null {
  if (!lineRange) return null;
  const match = /^(\d+)(?:-(\d+))?$/.exec(lineRange);
  if (!match) return null;
  const start = parseInt(match[1], 10);
  const end = match[2] ? parseInt(match[2], 10) : start;
  return isNaN(start) || isNaN(end) ? null : { start, end };
}

export function isFilePath(text: string): FilePathMatch {
  const notPath: FilePathMatch = {
    isPath: false,
    path: "",
    filename: "",
    extension: "",
  };

  if (!text || typeof text !== "string") return notPath;

  const trimmed = text.trim();
  if (!trimmed || trimmed.includes("\n")) return notPath;

  if (WEB_URL_RE.test(trimmed) || DANGEROUS_PROTOCOLS.test(trimmed) || trimmed.startsWith("mailto:")) {
    return notPath;
  }

  if (
    trimmed.includes(" ") ||
    trimmed.includes(";") ||
    trimmed.includes("(") ||
    trimmed.includes(")") ||
    trimmed.includes("{") ||
    trimmed.includes("}") ||
    trimmed.includes("==") ||
    trimmed.includes("!=") ||
    trimmed.includes("=>") ||
    trimmed.includes("+=") ||
    trimmed.includes("|")
  ) {
    return notPath;
  }

  let pathPart = trimmed;
  let lineRange: string | undefined;

  const anchorMatch = LINE_RANGE_RE.exec(trimmed);
  if (anchorMatch) {
    lineRange = anchorMatch[2] ? `${anchorMatch[1]}-${anchorMatch[2]}` : anchorMatch[1];
    pathPart = trimmed.slice(0, anchorMatch.index);
  }

  if (!pathPart) return notPath;

  const schemeStripped = pathPart.startsWith("file://")
    ? pathPart.slice("file://".length)
    : pathPart;

  const hasPathPrefix =
    pathPart.startsWith("file://") ||
    schemeStripped.startsWith("/") ||
    schemeStripped.startsWith("./") ||
    schemeStripped.startsWith("../") ||
    schemeStripped.startsWith("~/");

  const hasSeparators = schemeStripped.includes("/") || schemeStripped.includes("\\");

  const lastDot = schemeStripped.lastIndexOf(".");
  const lastSlash = Math.max(schemeStripped.lastIndexOf("/"), schemeStripped.lastIndexOf("\\"));
  const ext = lastDot > lastSlash ? schemeStripped.slice(lastDot).toLowerCase() : "";
  const filename = lastSlash >= 0 ? schemeStripped.slice(lastSlash + 1) : schemeStripped;

  const hasKnownExt = COMMON_FILE_EXTENSIONS.includes(ext);

  const isValid = hasPathPrefix || (hasSeparators && (hasKnownExt || !ext)) || hasKnownExt;

  if (!isValid || !filename) return notPath;

  return {
    isPath: true,
    path: pathPart,
    lineRange,
    filename,
    extension: ext,
  };
}

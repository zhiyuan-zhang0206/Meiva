export interface ImageDimensions {
  readonly width: number;
  readonly height: number;
}

const MAX_CACHE_ENTRIES = 1024;
const dimensionsCache = new Map<string, ImageDimensions>();

export function normalizeImageDimensions(
  dims: { readonly width?: number | null; readonly height?: number | null } | null | undefined,
): ImageDimensions | null {
  if (!dims) return null;
  const { width, height } = dims;
  if (
    typeof width !== "number" ||
    typeof height !== "number" ||
    !Number.isFinite(width) ||
    !Number.isFinite(height) ||
    width <= 0 ||
    height <= 0
  ) {
    return null;
  }
  return { width: Math.round(width), height: Math.round(height) };
}

export function getCachedImageDimensions(src: string): ImageDimensions | null {
  const hit = dimensionsCache.get(src);
  if (!hit) return null;
  dimensionsCache.delete(src);
  dimensionsCache.set(src, hit);
  return hit;
}

export function setCachedImageDimensions(src: string, width: number, height: number): void {
  const normalized = normalizeImageDimensions({ width, height });
  if (!normalized) return;
  dimensionsCache.delete(src);
  dimensionsCache.set(src, normalized);
  if (dimensionsCache.size > MAX_CACHE_ENTRIES) {
    const oldest = dimensionsCache.keys().next().value;
    if (oldest !== undefined) dimensionsCache.delete(oldest);
  }
}

export function clearCachedImageDimensions(): void {
  dimensionsCache.clear();
}
